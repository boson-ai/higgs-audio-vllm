# SPDX-License-Identifier: Apache-2.0
import asyncio
import base64
import io
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Callable, Final, Optional, Union

import jinja2
import numpy as np
import soundfile as sf
from fastapi import Request
from pydantic import TypeAdapter

from vllm.config import ModelConfig
from vllm.engine.protocol import EngineClient
from vllm.entrypoints.chat_utils import (ChatTemplateContentFormatOption,
                                         ConversationMessage)
from vllm.entrypoints.logger import RequestLogger
from vllm.entrypoints.openai.protocol import (
    ChatCompletionAudio, ChatCompletionNamedToolChoiceParam,
    ChatCompletionRequest, ChatCompletionResponse,
    ChatCompletionResponseChoice, ChatCompletionResponseStreamChoice,
    ChatCompletionStreamResponse, ChatMessage, DeltaFunctionCall, DeltaMessage,
    DeltaToolCall, ErrorResponse, FunctionCall, FunctionDefinition,
    PromptTokenUsageInfo, RequestResponseMetadata, ToolCall, UsageInfo)
from vllm.entrypoints.openai.serving_engine import (OpenAIServing,
                                                    clamp_prompt_logprobs)
from vllm.entrypoints.openai.serving_models import OpenAIServingModels
from vllm.entrypoints.openai.tool_parsers import ToolParser, ToolParserManager
from vllm.logger import init_logger
from vllm.model_executor.models.higgs_audio_tokenizer import (
    AudioTokenizer, revert_delay_pattern)
from vllm.outputs import CompletionOutput, RequestOutput
from vllm.reasoning import ReasoningParser, ReasoningParserManager
from vllm.sampling_params import BeamSearchParams, SamplingParams
from vllm.transformers_utils.tokenizer import AnyTokenizer
from vllm.utils import random_uuid

from .utils import create_audio_chunk, split_interleaved_delayed_audios

logger = init_logger(__name__)


def convert_audio_to_base64(audio: np.ndarray,
                            sampling_rate: int,
                            target_format: str = "wav") -> str:
    audio_buffer = io.BytesIO()
    sf.write(audio_buffer, audio, sampling_rate, format=target_format)
    return base64.b64encode(audio_buffer.getvalue()).decode('utf-8')


class HiggsAudioServingChat(OpenAIServing):

    def __init__(
        self,
        engine_client: EngineClient,
        model_config: ModelConfig,
        models: OpenAIServingModels,
        response_role: str,
        *,
        request_logger: Optional[RequestLogger],
        chat_template_content_format: ChatTemplateContentFormatOption,
        chat_template: Optional[str] = None,
        return_tokens_as_token_ids: bool = False,
        enable_reasoning: bool = False,
        reasoning_parser: Optional[str] = None,
        enable_auto_tools: bool = False,
        tool_parser: Optional[str] = None,
        enable_prompt_tokens_details: bool = False,
        audio_tokenizer: Optional[AudioTokenizer] = None,
    ):
        super().__init__(engine_client=engine_client,
                         model_config=model_config,
                         models=models,
                         request_logger=request_logger,
                         return_tokens_as_token_ids=return_tokens_as_token_ids)

        self.request_logger = request_logger
        self.response_role = response_role
        self.chat_template = chat_template
        self.chat_template_content_format: Final = chat_template_content_format
        self.enable_reasoning = enable_reasoning
        self.reasoning_parser = reasoning_parser

        # set up tool use
        self.enable_auto_tools: bool = enable_auto_tools
        if self.enable_auto_tools:
            logger.info(
                "\"auto\" tool choice has been enabled please note that while"
                " the parallel_tool_calls client option is preset for "
                "compatibility reasons, it will be ignored.")

        self.enable_reasoning: bool = enable_reasoning
        self.reasoning_parser: Optional[Callable[[AnyTokenizer],
                                                 ReasoningParser]] = None
        if self.enable_reasoning:
            try:
                self.reasoning_parser = (
                    ReasoningParserManager.get_reasoning_parser(
                        reasoning_parser))
            except Exception as e:
                raise TypeError("Error: --enable-reasoning requires "
                                f"reasoning_parser:'{reasoning_parser}' "
                                "which has not been registered") from e

        self.tool_parser: Optional[Callable[[AnyTokenizer], ToolParser]] = None
        if self.enable_auto_tools:
            try:
                if (tool_parser == "pythonic" and
                        model_config.model.startswith("meta-llama/Llama-3.2")):
                    logger.warning(
                        "Llama3.2 models may struggle to emit valid pythonic"
                        " tool calls")
                self.tool_parser = ToolParserManager.get_tool_parser(
                    tool_parser)
            except Exception as e:
                raise TypeError("Error: --enable-auto-tool-choice requires "
                                f"tool_parser:'{tool_parser}' which has not "
                                "been registered") from e

        self.enable_prompt_tokens_details = enable_prompt_tokens_details
        self.enable_prompt_tokens_details = enable_prompt_tokens_details
        self.default_sampling_params = (
            self.model_config.get_diff_sampling_param())
        if self.default_sampling_params:
            source = self.model_config.generation_config
            source = "model" if source == "auto" else source
            logger.info("Using default chat sampling params from %s: %s",
                        source, self.default_sampling_params)

        self.audio_tokenizer = audio_tokenizer
        self.audio_num_codebooks = self.audio_tokenizer.num_codebooks
        self.audio_stream_bos_id = model_config.hf_config.audio_stream_bos_id
        self.audio_stream_eos_id = model_config.hf_config.audio_stream_eos_id
        self.audio_codebook_size = self.audio_tokenizer.codebook_size
        self.audio_tokenizer_tps = self.audio_tokenizer.tps
        self.samples_per_token = int(self.audio_tokenizer.sampling_rate //
                                     self.audio_tokenizer_tps)

    # ruff: noqa: E501  # Disable specific lint rules
    def get_chat_template(self) -> str:
        if self.chat_template is not None:
            return self.chat_template

        # fmt: off
        return (
                "{% set loop_messages = messages %}"
                "{% for message in loop_messages %}"
                    "{% set content = '<|start_header_id|>' + message['role'] + "
                    "'<|end_header_id|>\n\n' + message['content'] | trim + '<|eot_id|>' %}"
                    "{% if loop.index0 == 0 %}"
                        "{% set content = bos_token + content %}"
                    "{% endif %}"
                    "{% if message['role'] == 'assistant' and '<|audio_bos|><|AUDIO|>' in message['content'] %}"
                        "{% set content = content.replace('<|audio_bos|><|AUDIO|>', '<|audio_out_bos|><|AUDIO|>') %}"
                    "{% endif %}"
                    "{{ content }}"
                "{% endfor %}"
                "{% if add_generation_prompt %}"
                    "{{ '<|start_header_id|>assistant<|end_header_id|>\n\n' }}"
                "{% endif %}"
            )
        # fmt: on

    async def chat_completion_stream_generator(
        self,
        request: ChatCompletionRequest,
        result_generator: AsyncIterator[RequestOutput],
        request_id: str,
        model_name: str,
        conversation: list[ConversationMessage],
        tokenizer: AnyTokenizer,
        request_metadata: RequestResponseMetadata,
    ) -> AsyncGenerator[str, None]:
        created_time = int(time.time())
        chunk_object_type: Final = "chat.completion.chunk"
        first_iteration = True

        # Send response for each token for each request.n (index)
        num_choices = 1 if request.n is None else request.n
        previous_num_tokens = [0] * num_choices
        finish_reason_sent = [False] * num_choices
        num_prompt_tokens = 0
        num_cached_tokens = None

        # Hold the audio token
        audio_tokens_cache = \
            [np.ndarray((0, self.audio_num_codebooks), dtype=np.int64) \
             for _ in range(num_choices)]
        is_first_audio_chunk = [True] * num_choices
        fade_out_audio = [None] * num_choices

        audio_chunk_size = self.audio_tokenizer_tps
        audio_chunk_overlap_size = self.audio_tokenizer_tps
        if request.audio is not None and hasattr(request.audio,
                                                 "audio_chunk_size"):
            audio_chunk_size = request.audio.audio_chunk_size or audio_chunk_size
            audio_chunk_overlap_size = \
                request.audio.audio_chunk_overlap_size or audio_chunk_overlap_size

        if isinstance(request.tool_choice, ChatCompletionNamedToolChoiceParam):
            tool_choice_function_name = request.tool_choice.function.name
        else:
            tool_choice_function_name = None

        # Determine whether tools are in use with "auto" tool choice
        tool_choice_auto = (
            not tool_choice_function_name
            and self._should_stream_with_auto_tool_parsing(request))

        should_stream_with_reasoning_parsing = (
            self._should_stream_with_reasoning_parsing(request))

        all_previous_token_ids: Optional[list[list[int]]]
        function_name_returned: Optional[list[bool]] = None

        # Only one of these will be used, thus previous_texts and
        # all_previous_token_ids will not be used twice in the same iteration.
        if tool_choice_auto or should_stream_with_reasoning_parsing:
            # These are only required in "auto" tool choice case
            previous_texts = [""] * num_choices
            all_previous_token_ids = [[]] * num_choices
            # For reasoning parser and tool call all enabled
            added_content_delta_arr = [False] * num_choices
            reasoning_end_arr = [False] * num_choices
        elif request.tool_choice == "required":
            previous_texts = [""] * num_choices
            function_name_returned = [False] * num_choices
            all_previous_token_ids = None
        else:
            previous_texts, all_previous_token_ids = None, None

        try:
            # There is no need to check if the reasoning_parser is None
            # because the should_stream_with_reasoning_parsing check
            # already ensures that the reasoning_parser is not None.
            # but the pre-commit hook requires it.
            if should_stream_with_reasoning_parsing and \
                self.reasoning_parser is not None:
                reasoning_parser = self.reasoning_parser(tokenizer)
        except RuntimeError as e:
            logger.exception("Error in reasoning parser creation.")
            data = self.create_streaming_error_response(str(e))
            yield f"data: {data}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Prepare the tool parser if it's needed
        try:
            if tool_choice_auto and self.tool_parser:
                tool_parsers: list[Optional[ToolParser]] = [
                    self.tool_parser(tokenizer)
                ] * num_choices
            else:
                tool_parsers = [None] * num_choices
        except Exception as e:
            logger.exception("Error in tool parser creation.")
            data = self.create_streaming_error_response(str(e))
            yield f"data: {data}\n\n"
            yield "data: [DONE]\n\n"
            return

        stream_options = request.stream_options
        if stream_options:
            include_usage = stream_options.include_usage
            include_continuous_usage = include_usage and \
                                       stream_options.continuous_usage_stats
        else:
            include_usage, include_continuous_usage = False, False

        try:
            async for res in result_generator:
                if res.prompt_token_ids is not None:
                    num_prompt_tokens = len(res.prompt_token_ids)
                    if res.encoder_prompt_token_ids is not None:
                        num_prompt_tokens += len(res.encoder_prompt_token_ids)

                # We need to do it here, because if there are exceptions in
                # the result_generator, it needs to be sent as the FIRST
                # response (by the try...catch).
                if first_iteration:
                    num_cached_tokens = res.num_cached_tokens
                    # Send first response for each request.n (index) with
                    # the role
                    role = self.get_chat_request_role(request)

                    # NOTE num_choices defaults to 1 so this usually executes
                    # once per request
                    for i in range(num_choices):
                        choice_data = ChatCompletionResponseStreamChoice(
                            index=i,
                            delta=DeltaMessage(
                                role=role,
                                content="",
                            ),
                            logprobs=None,
                            finish_reason=None)
                        chunk = ChatCompletionStreamResponse(
                            id=request_id,
                            object=chunk_object_type,
                            created=created_time,
                            choices=[choice_data],
                            model=model_name)

                        # if continuous usage stats are requested, add it
                        if include_continuous_usage:
                            chunk.usage = UsageInfo(
                                prompt_tokens=num_prompt_tokens,
                                completion_tokens=0,
                                total_tokens=num_prompt_tokens)

                        data = chunk.model_dump_json(exclude_unset=True)
                        yield f"data: {data}\n\n"

                    # Send response to echo the input portion of the
                    # last message
                    if request.echo:
                        last_msg_content: Union[str, list[dict[str, str]]] = ""
                        if conversation and "content" in conversation[
                                -1] and conversation[-1].get("role") == role:
                            last_msg_content = conversation[-1]["content"] or ""

                        if last_msg_content:
                            for i in range(num_choices):
                                choice_data = (
                                    ChatCompletionResponseStreamChoice(
                                        index=i,
                                        delta=DeltaMessage(
                                            content=last_msg_content),
                                        logprobs=None,
                                        finish_reason=None))
                                chunk = ChatCompletionStreamResponse(
                                    id=request_id,
                                    object=chunk_object_type,
                                    created=created_time,
                                    choices=[choice_data],
                                    model=model_name)
                                if include_continuous_usage:
                                    chunk.usage = UsageInfo(
                                        prompt_tokens=num_prompt_tokens,
                                        completion_tokens=0,
                                        total_tokens=num_prompt_tokens)

                                data = chunk.model_dump_json(
                                    exclude_unset=True)
                                yield f"data: {data}\n\n"
                    first_iteration = False

                for output in res.outputs:
                    i = output.index
                    tool_parser = tool_parsers[i]

                    if finish_reason_sent[i]:
                        continue

                    if request.logprobs and request.top_logprobs is not None:
                        assert output.logprobs is not None, (
                            "Did not output logprobs")
                        logprobs = self._create_chat_logprobs(
                            token_ids=output.token_ids,
                            top_logprobs=output.logprobs,
                            tokenizer=tokenizer,
                            num_output_top_logprobs=request.top_logprobs,
                            return_as_token_id=request.
                            return_tokens_as_token_ids,
                        )
                    else:
                        logprobs = None

                    # Process audio tokens
                    audio_chunk = None
                    if output.mm_token_ids is None:
                        if audio_tokens_cache[i].shape[0] > 0:
                            audio_chunk, fade_out_audio[i] = create_audio_chunk(
                                audio_tokens_cache[i],
                                audio_chunk_size,
                                fade_out_audio[i],
                                finalize=True,
                                audio_tokenizer=self.audio_tokenizer,
                                audio_codebook_size=self.audio_codebook_size,
                                samples_per_token=self.samples_per_token,
                                audio_num_codebooks=self.audio_num_codebooks,
                                audio_stream_bos_id=self.audio_stream_bos_id,
                                audio_stream_eos_id=self.audio_stream_eos_id,
                            )
                            audio_tokens_cache[i] = np.ndarray(
                                (0, self.audio_num_codebooks), dtype=np.int64)
                            fade_out_audio[i] = None
                            # Reset the flag for the next audio sequences
                            is_first_audio_chunk[i] = True
                    else:
                        audio_tokens_cache[i] = np.concatenate([
                            audio_tokens_cache[i],
                            output.mm_token_ids,
                        ],
                                                               axis=0)
                        curr_audio_chunk_size = audio_tokens_cache[i].shape[0]

                        # The first audio chunk is generated with with less tokens than other chunks
                        # to reduce the first audio latency
                        if is_first_audio_chunk[i] and \
                            curr_audio_chunk_size >= (audio_chunk_size + self.audio_num_codebooks - 1):
                            first_audio_chunk_size = \
                                int(audio_chunk_size - self.audio_num_codebooks + 1)
                            audio_chunk, fade_out_audio[i] = create_audio_chunk(
                                audio_tokens_cache[i],
                                first_audio_chunk_size,
                                fade_out_audio[i],
                                finalize=False,
                                audio_tokenizer=self.audio_tokenizer,
                                audio_codebook_size=self.audio_codebook_size,
                                samples_per_token=self.samples_per_token,
                                audio_num_codebooks=self.audio_num_codebooks,
                                audio_stream_bos_id=self.audio_stream_bos_id,
                                audio_stream_eos_id=self.audio_stream_eos_id,
                            )
                            audio_tokens_cache[i] = audio_tokens_cache[i][
                                first_audio_chunk_size:]
                            is_first_audio_chunk[i] = False
                        elif not is_first_audio_chunk[i] and \
                            curr_audio_chunk_size >= (audio_chunk_size + audio_chunk_overlap_size):
                            audio_chunk, fade_out_audio[i] = create_audio_chunk(
                                audio_tokens_cache[i],
                                audio_chunk_size,
                                fade_out_audio[i],
                                finalize=False,
                                audio_tokenizer=self.audio_tokenizer,
                                audio_codebook_size=self.audio_codebook_size,
                                samples_per_token=self.samples_per_token,
                                audio_num_codebooks=self.audio_num_codebooks,
                                audio_stream_bos_id=self.audio_stream_bos_id,
                                audio_stream_eos_id=self.audio_stream_eos_id,
                            )
                            audio_tokens_cache[i] = audio_tokens_cache[i][
                                audio_chunk_size:]

                    delta_text = output.text

                    if not delta_text and not output.token_ids and \
                        not previous_num_tokens[i]:
                        # Chunked prefill case, don't return empty chunks
                        continue

                    delta_message: Optional[DeltaMessage]

                    # just update previous_texts and previous_token_ids
                    if tool_choice_auto or should_stream_with_reasoning_parsing:
                        assert previous_texts is not None
                        assert all_previous_token_ids is not None
                        previous_text = previous_texts[i]
                        previous_token_ids = all_previous_token_ids[i]
                        current_text = previous_text + delta_text
                        current_token_ids = previous_token_ids + list(
                            output.token_ids)

                    # handle streaming deltas for tools with named tool_choice
                    if tool_choice_function_name:
                        if (self.enable_reasoning
                                and not reasoning_parser.is_reasoning_end(
                                    previous_token_ids)):
                            assert reasoning_parser is not None
                            delta_message = (
                                reasoning_parser.
                                extract_reasoning_content_streaming(
                                    previous_text,
                                    current_text,
                                    delta_text,
                                    previous_token_ids,
                                    current_token_ids,
                                    output.token_ids,
                                ))
                            # When encountering think end id in delta_token_ids,
                            # process the `content`. Only keep 'content',
                            # remove 'reasoning_content'
                            if reasoning_parser.is_reasoning_end(
                                    list(output.token_ids)):
                                if delta_message and delta_message.content:
                                    # This need to be added to next `delta_text`
                                    current_text = delta_message.content
                                    delta_message.content = None
                                else:
                                    current_text = ""
                        else:
                            # Just to add remaining `content`
                            if self.enable_reasoning:
                                delta_text = previous_text + delta_text
                                current_text = ""

                            delta_message = DeltaMessage(tool_calls=[
                                DeltaToolCall(function=DeltaFunctionCall(
                                    name=tool_choice_function_name,
                                    arguments=delta_text),
                                              index=i)
                            ])

                    elif request.tool_choice == "required":
                        assert previous_texts is not None
                        assert function_name_returned is not None
                        previous_text = previous_texts[i]
                        current_text = previous_text + delta_text
                        fn_name_returned = function_name_returned[i]

                        delta_message, function_name_returned[i] = (
                            self.extract_tool_call_required_streaming(
                                previous_text=previous_text,
                                current_text=current_text,
                                delta_text=delta_text,
                                function_name_returned=fn_name_returned))

                        # update the previous values for the next iteration
                        previous_texts[i] = current_text

                    # handle streaming deltas for tools with "auto" tool choice
                    # and reasoning parser
                    elif tool_choice_auto and self.enable_reasoning:
                        assert tool_parser is not None
                        assert reasoning_parser is not None
                        assert added_content_delta_arr is not None
                        assert reasoning_end_arr is not None
                        if not reasoning_end_arr[i]:
                            delta_message = (
                                reasoning_parser.
                                extract_reasoning_content_streaming(
                                    previous_text,
                                    current_text,
                                    delta_text,
                                    previous_token_ids,
                                    current_token_ids,
                                    output.token_ids,
                                ))

                            # When encountering think end id in delta_token_ids,
                            # set reasoning status to end.
                            # Remove the text and token ids related
                            # to 'reasoning_content'.
                            if reasoning_parser.is_reasoning_end(
                                    list(output.token_ids)):
                                reasoning_end_arr[i] = True
                                current_token_ids =  \
                                    reasoning_parser.extract_content_ids(
                                        list(output.token_ids))
                                if delta_message and delta_message.content:
                                    current_text = delta_message.content
                                    delta_message.content = None
                                else:
                                    current_text = ""

                        # handle tool calls only after reasoning is done,
                        else:
                            delta_token_ids = list(output.token_ids)
                            # First time to tool call,
                            # add the remaining text and token ids
                            # to delta from previous
                            if not added_content_delta_arr[i]:
                                added_content_delta_arr[i] = True
                                previous_text = ""
                                previous_token_ids = []
                                delta_text = current_text
                                delta_token_ids = current_token_ids

                            delta_message = (
                                tool_parser.extract_tool_calls_streaming(
                                    previous_text=previous_text,
                                    current_text=current_text,
                                    delta_text=delta_text,
                                    previous_token_ids=previous_token_ids,
                                    current_token_ids=current_token_ids,
                                    delta_token_ids=delta_token_ids,
                                    request=request))
                    # when only tool calls
                    elif tool_choice_auto:
                        assert tool_parser is not None
                        delta_message = (
                            tool_parser.extract_tool_calls_streaming(
                                previous_text=previous_text,
                                current_text=current_text,
                                delta_text=delta_text,
                                previous_token_ids=previous_token_ids,
                                current_token_ids=current_token_ids,
                                delta_token_ids=output.token_ids,
                                request=request))
                    # when only reasoning
                    elif self.enable_reasoning:
                        assert reasoning_parser is not None
                        delta_message = (reasoning_parser.
                                         extract_reasoning_content_streaming(
                                             previous_text,
                                             current_text,
                                             delta_text,
                                             previous_token_ids,
                                             current_token_ids,
                                             output.token_ids,
                                         ))
                    # handle streaming just a content delta
                    else:
                        delta_message = DeltaMessage(content=delta_text,
                                                     audio=audio_chunk)

                    # update the previous values for the next iteration
                    if tool_choice_auto or should_stream_with_reasoning_parsing:
                        assert previous_texts is not None
                        assert all_previous_token_ids is not None
                        previous_texts[i] = current_text
                        all_previous_token_ids[i] = current_token_ids

                    # set the previous values for the next iteration
                    previous_num_tokens[i] += len(output.token_ids)

                    # if the message delta is None (e.g. because it was a
                    # "control token" for tool calls or the parser otherwise
                    # wasn't ready to send a token, then
                    #   get the next token without streaming a chunk
                    if delta_message is None:
                        continue

                    if output.finish_reason is None:
                        # Send token-by-token response for each request.n
                        choice_data = ChatCompletionResponseStreamChoice(
                            index=i,
                            delta=delta_message,
                            logprobs=logprobs,
                            finish_reason=None,
                        )

                    # if the model is finished generating
                    else:
                        # check to make sure we haven't "forgotten" to stream
                        #   any tokens that were generated but previously
                        #   matched by partial json parsing
                        # only happens if we are NOT using guided decoding
                        auto_tools_called = False
                        if tool_parser:
                            auto_tools_called = len(
                                tool_parser.prev_tool_call_arr) > 0
                            index = len(tool_parser.prev_tool_call_arr
                                        ) - 1 if auto_tools_called else 0
                        else:
                            index = 0

                        if self._should_check_for_unstreamed_tool_arg_tokens(
                                delta_message, output) and tool_parser:
                            latest_delta_len = 0
                            if ((isinstance(
                                    delta_message.tool_calls[0].function,
                                    DeltaFunctionCall)) and isinstance(
                                        delta_message.tool_calls[0].function.
                                        arguments, str)):
                                latest_delta_len = len(
                                    delta_message.tool_calls[0].function.
                                    arguments)

                            # get the expected call based on partial JSON
                            # parsing which "autocompletes" the JSON
                            expected_call = json.dumps(
                                tool_parser.prev_tool_call_arr[index].get(
                                    "arguments", {}),
                                ensure_ascii=False)

                            # get what we've streamed so far for arguments
                            # for the current tool
                            actual_call = tool_parser.streamed_args_for_tool[
                                index]
                            if (latest_delta_len > 0):
                                actual_call = actual_call[:-latest_delta_len]

                            # check to see if there's anything left to stream
                            remaining_call = expected_call.replace(
                                actual_call, "", 1)
                            # set that as a delta message
                            delta_message = DeltaMessage(tool_calls=[
                                DeltaToolCall(index=index,
                                              function=DeltaFunctionCall(
                                                  arguments=remaining_call).
                                              model_dump(exclude_none=True))
                            ])

                        # Send the finish response for each request.n only once
                        choice_data = ChatCompletionResponseStreamChoice(
                            index=i,
                            delta=delta_message,
                            logprobs=logprobs,
                            finish_reason=output.finish_reason
                            if not auto_tools_called else "tool_calls",
                            stop_reason=output.stop_reason)

                        finish_reason_sent[i] = True

                    chunk = ChatCompletionStreamResponse(
                        id=request_id,
                        object=chunk_object_type,
                        created=created_time,
                        choices=[choice_data],
                        model=model_name)

                    # handle usage stats if requested & if continuous
                    if include_continuous_usage:
                        completion_tokens = previous_num_tokens[i]
                        chunk.usage = UsageInfo(
                            prompt_tokens=num_prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=num_prompt_tokens + completion_tokens,
                        )

                    data = chunk.model_dump_json(exclude_unset=True)
                    yield f"data: {data}\n\n"

            # Process any remaining audio tokens if any
            for i in range(num_choices):
                if audio_tokens_cache[i].shape[0] > 0:
                    audio_chunk, fade_out_audio[i] = create_audio_chunk(
                        audio_tokens_cache[i],
                        audio_chunk_size,
                        fade_out_audio[i],
                        audio_tokenizer=self.audio_tokenizer,
                        audio_codebook_size=self.audio_codebook_size,
                        samples_per_token=self.samples_per_token,
                        audio_num_codebooks=self.audio_num_codebooks,
                        audio_stream_bos_id=self.audio_stream_bos_id,
                        audio_stream_eos_id=self.audio_stream_eos_id,
                        finalize=True)
                    if audio_chunk is not None:
                        delta_message = DeltaMessage(audio=audio_chunk)
                        choice_data = ChatCompletionResponseStreamChoice(
                            index=i, delta=delta_message, finish_reason=None)
                        chunk = ChatCompletionStreamResponse(
                            id=request_id,
                            object=chunk_object_type,
                            created=created_time,
                            choices=[choice_data],
                            model=model_name)
                        data = chunk.model_dump_json(exclude_unset=True)
                        yield f"data: {data}\n\n"

            # once the final token is handled, if stream_options.include_usage
            # is sent, send the usage
            if include_usage:
                completion_tokens = sum(previous_num_tokens)
                final_usage = UsageInfo(prompt_tokens=num_prompt_tokens,
                                        completion_tokens=completion_tokens,
                                        total_tokens=num_prompt_tokens +
                                        completion_tokens)
                if self.enable_prompt_tokens_details and num_cached_tokens:
                    final_usage.prompt_tokens_details = PromptTokenUsageInfo(
                        cached_tokens=num_cached_tokens)

                final_usage_chunk = ChatCompletionStreamResponse(
                    id=request_id,
                    object=chunk_object_type,
                    created=created_time,
                    choices=[],
                    model=model_name,
                    usage=final_usage)
                final_usage_data = (final_usage_chunk.model_dump_json(
                    exclude_unset=True, exclude_none=True))
                yield f"data: {final_usage_data}\n\n"

            # report to FastAPI middleware aggregate usage across all choices
            num_completion_tokens = sum(previous_num_tokens)
            request_metadata.final_usage_info = UsageInfo(
                prompt_tokens=num_prompt_tokens,
                completion_tokens=num_completion_tokens,
                total_tokens=num_prompt_tokens + num_completion_tokens)

        except Exception as e:
            # TODO: Use a vllm-specific Validation Error
            logger.exception("Error in chat completion stream generator.")
            data = self.create_streaming_error_response(str(e))
            yield f"data: {data}\n\n"
        # Send the final done message after all response.n are finished
        yield "data: [DONE]\n\n"

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        raw_request: Optional[Request] = None,
    ) -> Union[AsyncGenerator[str, None], ChatCompletionResponse,
               ErrorResponse]:
        error_check_ret = await self._check_model(request)
        if error_check_ret is not None:
            logger.error("Error with model %s", error_check_ret)
            return error_check_ret

        if self.engine_client.errored:
            raise self.engine_client.dead_error

        try:
            (
                lora_request,
                prompt_adapter_request,
            ) = self._maybe_get_adapters(request)

            model_name = self._get_model_name(request.model, lora_request)

            tokenizer = await self.engine_client.get_tokenizer(lora_request)

            tool_parser = self.tool_parser

            if (request.tool_choice == "auto" and
                    not (self.enable_auto_tools and tool_parser is not None)):
                # for hf tokenizers, "auto" tools requires
                # --enable-auto-tool-choice and --tool-call-parser
                return self.create_error_response(
                    "\"auto\" tool choice requires "
                    "--enable-auto-tool-choice and --tool-call-parser to be set"
                )

            tool_dicts = None if request.tools is None else [
                tool.model_dump() for tool in request.tools
            ]

            chat_template = request.chat_template or \
                self.get_chat_template()
            (
                conversation,
                request_prompts,
                engine_prompts,
            ) = await self._preprocess_chat(
                request,
                tokenizer,
                request.messages,
                chat_template=chat_template,
                chat_template_content_format=self.chat_template_content_format,
                add_generation_prompt=request.add_generation_prompt,
                continue_final_message=request.continue_final_message,
                tool_dicts=tool_dicts,
                documents=request.documents,
                chat_template_kwargs=request.chat_template_kwargs,
                tool_parser=tool_parser,
                truncate_prompt_tokens=request.truncate_prompt_tokens,
                add_special_tokens=request.add_special_tokens,
            )
        except (ValueError, TypeError, RuntimeError,
                jinja2.TemplateError) as e:
            logger.exception("Error in preprocessing prompt inputs")
            return self.create_error_response(str(e))

        request_id = "chatcmpl-" \
                     f"{self._base_request_id(raw_request, request.request_id)}"
        request_metadata = RequestResponseMetadata(request_id=request_id)
        if raw_request:
            raw_request.state.request_metadata = request_metadata

        # Schedule the request and get the result generator.
        generators: list[AsyncGenerator[RequestOutput, None]] = []
        try:
            for i, engine_prompt in enumerate(engine_prompts):
                sampling_params: Union[SamplingParams, BeamSearchParams]
                default_max_tokens = self.max_model_len - len(
                    engine_prompt["prompt_token_ids"])
                if request.use_beam_search:
                    sampling_params = request.to_beam_search_params(
                        default_max_tokens, self.default_sampling_params)
                else:
                    sampling_params = request.to_sampling_params(
                        default_max_tokens,
                        self.model_config.logits_processor_pattern,
                        self.default_sampling_params)

                self._log_inputs(request_id,
                                 request_prompts[i],
                                 params=sampling_params,
                                 lora_request=lora_request,
                                 prompt_adapter_request=prompt_adapter_request)

                trace_headers = (None if raw_request is None else await
                                 self._get_trace_headers(raw_request.headers))

                if isinstance(sampling_params, BeamSearchParams):
                    generator = self.engine_client.beam_search(
                        prompt=engine_prompt,
                        request_id=request_id,
                        params=sampling_params,
                    )
                else:
                    generator = self.engine_client.generate(
                        engine_prompt,
                        sampling_params,
                        request_id,
                        lora_request=lora_request,
                        trace_headers=trace_headers,
                        prompt_adapter_request=prompt_adapter_request,
                        priority=request.priority,
                    )

                generators.append(generator)
        except ValueError as e:
            # TODO: Use a vllm-specific Validation Error
            return self.create_error_response(str(e))

        assert len(generators) == 1
        result_generator, = generators

        # Streaming response
        if request.stream:
            return self.chat_completion_stream_generator(
                request, result_generator, request_id, model_name,
                conversation, tokenizer, request_metadata)

        try:
            return await self.chat_completion_full_generator(
                request, result_generator, request_id, model_name,
                conversation, tokenizer, request_metadata)
        except ValueError as e:
            # TODO: Use a vllm-specific Validation Error
            return self.create_error_response(str(e))

    def get_chat_request_role(self, request: ChatCompletionRequest) -> str:
        if request.add_generation_prompt:
            return self.response_role
        return request.messages[-1]["role"]

    async def chat_completion_full_generator(
        self,
        request: ChatCompletionRequest,
        result_generator: AsyncIterator[RequestOutput],
        request_id: str,
        model_name: str,
        conversation: list[ConversationMessage],
        tokenizer: AnyTokenizer,
        request_metadata: RequestResponseMetadata,
    ) -> Union[ErrorResponse, ChatCompletionResponse]:

        created_time = int(time.time())
        final_res: Optional[RequestOutput] = None

        try:
            async for res in result_generator:
                final_res = res
        except asyncio.CancelledError:
            return self.create_error_response("Client disconnected")
        except ValueError as e:
            # TODO: Use a vllm-specific Validation Error
            return self.create_error_response(str(e))

        assert final_res is not None

        choices: list[ChatCompletionResponseChoice] = []

        role = self.get_chat_request_role(request)
        for output in final_res.outputs:
            token_ids = output.token_ids
            out_logprobs = output.logprobs

            if request.logprobs and request.top_logprobs is not None:
                assert out_logprobs is not None, "Did not output logprobs"
                logprobs = self._create_chat_logprobs(
                    token_ids=token_ids,
                    top_logprobs=out_logprobs,
                    num_output_top_logprobs=request.top_logprobs,
                    tokenizer=tokenizer,
                    return_as_token_id=request.return_tokens_as_token_ids,
                )
            else:
                logprobs = None

            should_stream_with_reasoning_parsing = (
                self._should_stream_with_reasoning_parsing(request))

            # In the OpenAI API the finish_reason is "tools_called"
            # if the tool choice is auto and the model produced a tool
            # call. The same is not true for named function calls
            auto_tools_called = False

            if should_stream_with_reasoning_parsing and \
                self.reasoning_parser is not None:
                try:
                    reasoning_parser = self.reasoning_parser(tokenizer)
                except RuntimeError as e:
                    logger.exception("Error in reasoning parser creation.")
                    return self.create_error_response(str(e))
                # If the reasoning parser is enabled,
                # tool calls are extracted exclusively from the content.
                reasoning_content, content = (
                    reasoning_parser.extract_reasoning_content(
                        output.text, request=request))
            else:
                reasoning_content = None
                content = output.text

            # Post-process the audio tokens to audio waveform
            if output.mm_token_ids is not None:
                audio_datas = split_interleaved_delayed_audios(
                    output.mm_token_ids, self.audio_tokenizer,
                    self.audio_stream_eos_id)

                wv_list = []
                for audio_data in audio_datas:
                    audio_data = np.array(audio_data, dtype=np.int64)[1:-1, :]
                    reverted_audio_data = \
                        revert_delay_pattern(audio_data.transpose(1, 0))
                    reverted_audio_data = \
                        reverted_audio_data.clip(0, self.audio_codebook_size - 1)
                    wv, sampling_rate = self.audio_tokenizer.decode(
                        vq_code=reverted_audio_data)
                    wv_list.append(wv)
                wv_numpy = np.concatenate(wv_list)

                # Convert audio to base64
                response_audio_format = "wav" if request.audio is None \
                                        else request.audio.format
                audio_base64 = convert_audio_to_base64(wv_numpy, sampling_rate,
                                                       response_audio_format)
            else:
                audio_base64 = None

            # if auto tools are not enabled, and a named tool choice using
            #   outlines is not being used
            if (not self.enable_auto_tools or not self.tool_parser) and \
                (not isinstance(request.tool_choice,
                                ChatCompletionNamedToolChoiceParam
                                ) and request.tool_choice != "required"):
                message = ChatMessage(
                    role=role,
                    reasoning_content=reasoning_content,
                    content=content,
                    audio=ChatCompletionAudio(id=f"audio-{random_uuid()}",
                                              data=audio_base64,
                                              expires_at=0,
                                              transcript=""),
                )

            # if the request uses tools and specified a tool choice
            elif request.tool_choice and type(
                    request.tool_choice) is ChatCompletionNamedToolChoiceParam:

                tool_call_class = ToolCall
                message = ChatMessage(
                    role=role,
                    reasoning_content=reasoning_content,
                    content="",
                    tool_calls=[
                        tool_call_class(function=FunctionCall(
                            name=request.tool_choice.function.name,
                            arguments=content))
                    ])

            elif request.tool_choice and request.tool_choice == "required":
                tool_call_class = ToolCall

                # the fields of FunctionDefinition are a superset of the
                # tool call outputs and can be used for parsing
                tool_calls = TypeAdapter(
                    list[FunctionDefinition]).validate_json(output.text)
                message = ChatMessage(
                    role=role,
                    content="",
                    tool_calls=[
                        tool_call_class(function=FunctionCall(
                            name=tool_call.name,
                            arguments=json.dumps(tool_call.parameters)))
                        for tool_call in tool_calls
                    ])

            # if the request doesn't use tool choice
            # OR specifies to not use a tool
            elif not request.tool_choice or request.tool_choice == "none":

                message = ChatMessage(role=role,
                                      reasoning_content=reasoning_content,
                                      content=content,
                                      audio=ChatCompletionAudio(
                                          id=f"audio-{random_uuid()}",
                                          data=audio_base64,
                                          expires_at=0,
                                          transcript=""))

            # handle when there are tools and tool choice is auto
            elif request.tools and (
                    request.tool_choice == "auto"
                    or request.tool_choice is None) and self.enable_auto_tools \
                    and self.tool_parser:

                try:
                    tool_parser = self.tool_parser(tokenizer)
                except RuntimeError as e:
                    logger.exception("Error in tool parser creation.")
                    return self.create_error_response(str(e))

                tool_call_info = tool_parser.extract_tool_calls(
                    content if content is not None else "", request=request)
                # In the OpenAI API the finish_reason is "tools_called"
                # if the tool choice is auto and the model produced a tool
                # call. The same is not true for named function calls
                auto_tools_called = tool_call_info.tools_called
                if tool_call_info.tools_called:
                    message = ChatMessage(role=role,
                                          reasoning_content=reasoning_content,
                                          content=tool_call_info.content,
                                          tool_calls=tool_call_info.tool_calls)

                else:
                    # FOR NOW make it a chat message; we will have to detect
                    # the type to make it later.
                    message = ChatMessage(role=role,
                                          reasoning_content=reasoning_content,
                                          content=content)

            # undetermined case that is still important to handle
            else:
                logger.error(
                    "Error in chat_completion_full_generator - cannot determine"
                    " if tools should be extracted. Returning a standard chat "
                    "completion.")
                message = ChatMessage(role=role,
                                      reasoning_content=reasoning_content,
                                      content=content,
                                      audio=ChatCompletionAudio(
                                          id=f"audio-{random_uuid()}",
                                          data=audio_base64,
                                          expires_at=0,
                                          transcript=""))

            choice_data = ChatCompletionResponseChoice(
                index=output.index,
                message=message,
                logprobs=logprobs,
                finish_reason="tool_calls" if auto_tools_called else
                output.finish_reason if output.finish_reason else "stop",
                stop_reason=output.stop_reason)
            choices.append(choice_data)

        if request.echo:
            last_msg_content: Union[str, list[dict[str, str]]] = ""
            if conversation and "content" in conversation[-1] and conversation[
                    -1].get("role") == role:
                last_msg_content = conversation[-1]["content"] or ""
            if isinstance(last_msg_content, list):
                last_msg_content = "\n".join(msg['text']
                                             for msg in last_msg_content)

            for choice in choices:
                full_message = last_msg_content + (choice.message.content
                                                   or "")
                choice.message.content = full_message

        assert final_res.prompt_token_ids is not None
        num_prompt_tokens = len(final_res.prompt_token_ids)
        if final_res.encoder_prompt_token_ids is not None:
            num_prompt_tokens += len(final_res.encoder_prompt_token_ids)
        num_generated_tokens = sum(
            len(output.token_ids) for output in final_res.outputs)
        usage = UsageInfo(prompt_tokens=num_prompt_tokens,
                          completion_tokens=num_generated_tokens,
                          total_tokens=num_prompt_tokens +
                          num_generated_tokens)
        if self.enable_prompt_tokens_details and final_res.num_cached_tokens:
            usage.prompt_tokens_details = PromptTokenUsageInfo(
                cached_tokens=final_res.num_cached_tokens)

        request_metadata.final_usage_info = usage

        response = ChatCompletionResponse(
            id=request_id,
            created=created_time,
            model=model_name,
            choices=choices,
            usage=usage,
            prompt_logprobs=clamp_prompt_logprobs(final_res.prompt_logprobs),
        )

        return response

    def _should_stream_with_auto_tool_parsing(self,
                                              request: ChatCompletionRequest):
        """
        Utility function to check if streamed tokens should go through the tool
        call parser that was configured.

        We only want to do this IF user-provided tools are set, a tool parser
        is configured, "auto" tool choice is enabled, and the request's tool
        choice field indicates that "auto" tool choice should be used.
        """
        return (request.tools and self.tool_parser and self.enable_auto_tools
                and request.tool_choice in ['auto', None])

    def _should_stream_with_reasoning_parsing(self,
                                              request: ChatCompletionRequest):
        """
            Utility function to check if streamed tokens should go through the
            reasoning parser that was configured.
    
            We only want to do this IF reasoning is enabled and a reasoning 
            parser is configured.
            """
        return self.enable_reasoning and self.reasoning_parser is not None

    def _should_check_for_unstreamed_tool_arg_tokens(
        self,
        delta_message: Optional[DeltaMessage],
        output: CompletionOutput,
    ) -> bool:
        """
        Check to see if we should check for unstreamed tool arguments tokens.
        This is only applicable when auto tool parsing is enabled, the delta
        is a tool call with arguments.
        """

        # yapf: disable
        return bool(
            # if there is a delta message that includes tool calls which
            # include a function that has arguments
            output.finish_reason is not None
            and self.enable_auto_tools and self.tool_parser and delta_message
            and delta_message.tool_calls and delta_message.tool_calls[0]
            and delta_message.tool_calls[0].function
            and delta_message.tool_calls[0].function.arguments is not None
        )
