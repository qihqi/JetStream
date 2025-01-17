# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Benchmark JetStream online serving.

On the server side, run one of the following commands:
    * For real server, you need to pass correct server config (include the model config that 
      being passed into your engine impl) to the command below. Refer to config_lib.py and 
      implementations/mock/config.py for config impl detail.

    (run with real server)
    python -m jetstream.core.implementations.<your_impl>.server \
        --config <your_server_config>

    (run with mock server)
    python -m jetstream.core.implementations.mock.server

On the client side, run:
    * For real server and shareGPT dataset, you need to pass the tokenizer, server config, and
      dataset flags to the command below, and make some changes to the tokenizer logic in the 
      benchmark script (get_tokenizer and sample_requests func) to use your tokenizer correctly.
    * Add `--save-result` flag to save the benchmark result to a json file in current folder.
    * Add `--threads` flag to set the maximum number of threads used for request dispatching.

    (run with real model and engines)
    python -m benchmarks.benchmark_serving \
        --tokenizer <your_tokenizer> --dataset <target_dataset_path> \
        --request-rate <request_rate>

    (run with mock)
    python -m benchmarks.benchmark_serving \
        --request-rate 1

e2e example: python3 benchmark_serving.py --tokenizer /home/rwitten/maxtext/assets/tokenizer --num-prompts 100  --dataset ~/ShareGPT_V3_unfiltered_cleaned_split.json 
"""


import tensorflow as tf
import tensorflow_text as tftxt

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import json
import random
import time
from typing import Any, AsyncGenerator, List, Tuple
import grpc
from jetstream.core.proto import jetstream_pb2
from jetstream.core.proto import jetstream_pb2_grpc
import numpy as np
from tqdm.asyncio import tqdm


@dataclass
class BenchmarkMetrics:
  completed: int
  total_input: int
  total_output: int
  request_throughput: float
  input_throughput: float
  output_throughput: float
  mean_ttft_ms: float
  median_ttft_ms: float
  p99_ttft_ms: float
  mean_tpot_ms: float
  median_tpot_ms: float
  p99_tpot_ms: float


@dataclass
class InputRequest:
  prompt: str = ""
  prompt_len: int = 0
  output: str = ""
  output_len: int = 0

@dataclass
class RequestFuncOutput:
  input_request: InputRequest = None
  generated_token_list: list[str] = None
  success: bool = False
  latency: float = 0
  ttft: float = 0
  prompt_len: int = 0

  # Flatten the structure and return only the necessary results
  def to_dict(self): 
    return {
      "prompt": self.input_request.prompt,
      "original_output": self.input_request.output,
      "generated_token_list": self.generated_token_list,
      "success": self.success,
      "latency": self.latency,
      "prompt_len": self.prompt_len
    }  


def get_tokenizer(tokenizer_name: str) -> Any:
  """Return a tokenizer or a tokenizer placholder."""
  if tokenizer_name == "test":
    return "test"
  else:
    with tf.io.gfile.GFile(tokenizer_name, 'rb') as model_fp:
      sp_model = model_fp.read()
    sp_tokenizer = tftxt.SentencepieceTokenizer(
        model=sp_model, add_bos=True, add_eos=False, reverse=False)
    return sp_tokenizer

def sample_requests(
    dataset_path: str,
    num_requests: int,
    tokenizer: Any,
    max_output_length: int,
) -> List[InputRequest]:
  # Load the dataset.
  with open(dataset_path) as f:
    dataset = json.load(f)
  # Filter out the conversations with less than 2 turns.
  dataset = [data for data in dataset if len(data["conversations"]) >= 2]
  # Only keep the first two turns of each conversation.
  dataset = [
      (data["conversations"][0]["value"], data["conversations"][1]["value"])
      for data in dataset
  ]

  # some of these will be filtered out, so sample more than we need
  sampled_indices = random.sample(range(len(dataset)), int(num_requests * 1.2))
  dataset = [dataset[i] for i in sampled_indices]

  # Tokenize the prompts and completions.
  prompts = [prompt for prompt, _ in dataset]
  prompt_token_ids = tokenizer.tokenize(
      prompts
  )  # adjust this code based on tokenizer method
  completions = [completion for _, completion in dataset]
  completion_token_ids = tokenizer.tokenize(
      completions
  )  # adjust this code based on tokenizer method
  tokenized_dataset = []
  for i in range(len(dataset)):
    output_len = len(completion_token_ids[i])
    tokenized_dataset.append((prompts[i], prompt_token_ids[i], completions[i], output_len))

  # Filter out too long sequences.
  filtered_dataset: List[InputRequest] = []

  for prompt, prompt_token_ids, output, output_len in tokenized_dataset:
    prompt_len = len(prompt_token_ids)
    if prompt_len < 4 or output_len < 4:
      # Prune too short sequences.
      # This is because TGI causes errors when the input or output length
      # is too short.
      continue
    if prompt_len > 1024 or prompt_len + output_len > 2048:
      # Prune too long sequences.
      continue
    reqeust = InputRequest(prompt, prompt_len, output, max_output_length)
    filtered_dataset.append(reqeust)

  # Sample the requests.
  sampled_requests = random.sample(filtered_dataset, num_requests)
  return sampled_requests


async def get_request(
    input_requests: List[InputRequest],
    request_rate: float,
) -> AsyncGenerator[InputRequest, None]:
  input_requests = iter(input_requests)
  for request in input_requests:
    yield request

    if request_rate == float("inf"):
      # If the request rate is infinity, then we don't need to wait.
      continue
    # Sample the request interval from the exponential distribution.
    interval = np.random.exponential(1.0 / request_rate)
    # The next request will be sent after the interval.
    await asyncio.sleep(interval)


def calculate_metrics(
    input_requests: List[InputRequest],
    outputs: List[RequestFuncOutput],
    dur_s: float,
    tokenizer: Any,
) -> BenchmarkMetrics:
  total_output = 0
  total_input = 0
  completed = 0
  per_token_latencies = []
  ttfts = []
  for i in range(len(outputs)):
    if outputs[i].success:
      output_len = len(
          outputs[i].generated_token_list
          if tokenizer != "test"
          else ["Ċ", "Ō", "Ɵ"]
      )
      total_output += output_len
      total_input += input_requests[i].prompt_len
      per_token_latencies.append(outputs[i].latency / output_len)
      ttfts.append(outputs[i].ttft)
      completed += 1

  metrics = BenchmarkMetrics(
      completed=completed,
      total_input=total_input,
      total_output=total_output,
      request_throughput=completed / dur_s,
      input_throughput=total_input / dur_s,
      output_throughput=total_output / dur_s,
      mean_ttft_ms=np.mean(ttfts) * 1000,
      median_ttft_ms=np.median(ttfts) * 1000,
      p99_ttft_ms=np.percentile(ttfts, 99) * 1000,
      mean_tpot_ms=np.mean(per_token_latencies) * 1000,
      median_tpot_ms=np.median(per_token_latencies) * 1000,
      p99_tpot_ms=np.percentile(per_token_latencies, 99) * 1000,
  )

  return metrics


def grpc_sync_request(api_url: str, request: Any) -> tuple[list[str], float, float]:
  """Send grpc synchronous request since the current grpc server is sync."""
  with grpc.insecure_channel(api_url) as channel:
    grpc.channel_ready_future(channel).result()
    stub = jetstream_pb2_grpc.OrchestratorStub(channel)
    print("Making request")
    ttft = 0
    token_list = []
    request_start_time = time.perf_counter()
    response = stub.Decode(request)
    for token in response:
      if ttft == 0:
        ttft = time.perf_counter() - request_start_time
      token_list.append(token.response[0])
    latency = time.perf_counter() - request_start_time
    return token_list, ttft, latency


async def send_request(
    api_url: str,
    input_request: InputRequest,
    pbar: tqdm,
    session_cache: str,
    priority: int,
    threads: int,
) -> RequestFuncOutput:
  """Send the request to JetStream server."""
  loop = asyncio.get_running_loop()
  loop.set_default_executor(ThreadPoolExecutor(max_workers=threads))
  request = jetstream_pb2.DecodeRequest(
      session_cache=session_cache,
      additional_text=input_request.prompt,
      priority=priority,
      max_tokens=input_request.output_len,
  )
  output = RequestFuncOutput()
  output.input_request = input_request
  output.prompt_len = input_request.prompt_len
  generated_token_list, ttft, latency = await loop.run_in_executor(
      None, grpc_sync_request, api_url, request
  )
  output.ttft = ttft
  output.latency = latency
  output.generated_token_list = generated_token_list
  output.success = True
  if pbar:
    pbar.update(1)
  return output


async def benchmark(
    api_url: str,
    tokenizer: Any,
    input_requests: List[InputRequest],
    request_rate: float,
    disable_tqdm: bool,
    session_cache: str,
    priority: int,
    threads: int,
):
  """Benchmark the online serving performance."""
  pbar = None if disable_tqdm else tqdm(total=len(input_requests))

  print(f"Traffic request rate: {request_rate}")

  benchmark_start_time = time.perf_counter()
  tasks = []
  async for request in get_request(input_requests, request_rate):
    tasks.append(
        asyncio.create_task(
            send_request(
                api_url=api_url,
                input_request=request,
                pbar=pbar,
                session_cache=session_cache,
                priority=priority,
                threads=threads,
            )
        )
    )
  outputs = await asyncio.gather(*tasks)

  if not disable_tqdm:
    pbar.close()

  benchmark_duration = time.perf_counter() - benchmark_start_time

  metrics = calculate_metrics(
      input_requests=input_requests,
      outputs=outputs,
      dur_s=benchmark_duration,
      tokenizer=tokenizer,
  )

  print(f"Successful requests: {metrics.completed}")
  print(f"Benchmark duration: {benchmark_duration:2f} s")
  print(f"Total input tokens: {metrics.total_input}")
  print(f"Total generated tokens: {metrics.total_output}")
  print(f"Request throughput: {metrics.request_throughput:.2f} requests/s")
  print(f"Input token throughput: {metrics.input_throughput:.2f} tokens/s")
  print(f"Output token throughput: {metrics.output_throughput:.2f} tokens/s")
  print(f"Mean TTFT: {metrics.mean_ttft_ms:.2f} ms")
  print(f"Median TTFT: {metrics.median_ttft_ms:.2f} ms")
  print(f"P99 TTFT: {metrics.p99_ttft_ms:.2f} ms")
  print(f"Mean TPOT: {metrics.mean_tpot_ms:.2f} ms")
  print(f"Median TPOT: {metrics.median_tpot_ms:.2f} ms")
  print(f"P99 TPOT: {metrics.p99_tpot_ms:.2f} ms")

  result = {
      "duration": benchmark_duration,
      "completed": metrics.completed,
      "total_input_tokens": metrics.total_input,
      "total_output_tokens": metrics.total_output,
      "request_inthroughput": metrics.request_throughput,
      "input_throughput": metrics.input_throughput,
      "output_throughput": metrics.output_throughput,
      "mean_ttft_ms": metrics.mean_ttft_ms,
      "median_ttft_ms": metrics.median_ttft_ms,
      "p99_ttft_ms": metrics.p99_ttft_ms,
      "mean_tpot_ms": metrics.mean_tpot_ms,
      "median_tpot_ms": metrics.median_tpot_ms,
      "p99_tpot_ms": metrics.p99_tpot_ms,
  }
  return result, outputs


def mock_requests(total_mock_requests: int):
  """Generates a list of mock requests containing mock data."""
  data = []
  for _ in range(total_mock_requests):
    reqeust = InputRequest()
    reqeust.prompt = f"Prompt {random.randint(1, 1000)}"
    reqeust.prompt_len = random.randint(10, 100)
    reqeust.out = f"Output {random.randint(1, 1000)}"
    reqeust.output_len = random.randint(1, 10)
    data.append(reqeust)
  return data


def main(args: argparse.Namespace):
  print(args)
  random.seed(args.seed)
  np.random.seed(args.seed)

  model_id = args.model
  tokenizer_id = args.tokenizer

  api_url = f"{args.server}:{args.port}"

  tokenizer = get_tokenizer(tokenizer_id)
  if tokenizer == "test" or args.dataset == "test":
    input_requests = mock_requests(args.total_mock_requests) # e.g. [("AB", 2, "AB", 3)]
  else:
    input_requests = sample_requests(args.dataset, args.num_prompts, tokenizer, args.max_output_length)

  benchmark_result, request_outputs = asyncio.run(
      benchmark(
          api_url=api_url,
          tokenizer=tokenizer,
          input_requests=input_requests,
          request_rate=args.request_rate,
          disable_tqdm=args.disable_tqdm,
          session_cache=args.session_cache,
          priority=args.priority,
          threads=args.threads,
      )
  )

  # Save config and results to json
  if args.save_result:
    result_json = {}

    # Setup
    current_dt = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_json["date"] = current_dt
    result_json["model_id"] = model_id
    result_json["tokenizer_id"] = tokenizer_id
    result_json["num_prompts"] = args.num_prompts

    # Traffic
    result_json["request_rate"] = (
        args.request_rate if args.request_rate < float("inf") else "inf"
    )

    # Merge with benchmark result
    result_json = {**result_json, **benchmark_result}

    # Save to file
    base_model_id = model_id.split("/")[-1]
    file_name = (
        f"JetStream-{args.request_rate}qps-{base_model_id}-{current_dt}.json"
    )
    with open(file_name, "w") as outfile:
      json.dump(result_json, outfile)

  if args.save_request_outputs:
    file_path = args.request_outputs_file_path
    with open(file_path, "w") as output_file:
        json.dump([output.to_dict() for output in request_outputs], output_file, indent=4) 


if __name__ == "__main__":
  parser = argparse.ArgumentParser(
      description="Benchmark the online serving throughput."
  )
  parser.add_argument(
      "--server",
      type=str,
      default="dns:///[::1]",
      help="Server address.",
  )
  parser.add_argument("--port", type=str, default=9000)
  parser.add_argument(
      "--dataset", type=str, default="test", help="Path to the dataset."
  )
  parser.add_argument(
      "--model",
      type=str,
      default="no_model",
      help=(
          "Name of the model. (it's just used to label the benchmark, the model"
          " config is defined in config_lib, and passed as the server config"
          " flag when we run the JetStream server)"
      ),
  )
  parser.add_argument(
      "--tokenizer",
      type=str,
      default="test",
      help=(
          "Name or path of the tokenizer. (For mock model testing, use the"
          " default value)"
      ),
  )
  parser.add_argument(
      "--num-prompts",
      type=int,
      default=1000,
      help=(
          "Number of prompts to process. (number of sample requests we randomly"
          " collect from dataset)"
      ),
  )
  parser.add_argument(
      "--request-rate",
      type=float,
      default=float("inf"),
      help=(
          "Number of requests per second. If this is inf, "
          "then all the requests are sent at time 0. "
          "Otherwise, we use Poisson process to synthesize "
          "the request arrival times."
      ),
  )
  parser.add_argument(
      "--threads",
      type=int,
      default=110,
      help="The maximum number of threads used for request dispatching.",
  )
  parser.add_argument(
      "--total-mock-requests",
      type=int,
      default=150,
      help="The maximum number of mock requests to send for benchmark testing.",
  )

  parser.add_argument(
      "--max-output-length",
      type=int,
      default=1024,
      help="The maximum output length for reference request.",
  )

  parser.add_argument("--seed", type=int, default=0)
  parser.add_argument(
      "--disable-tqdm",
      action="store_true",
      help="Specify to disable tqdm progress bar.",
  )
  parser.add_argument(
      "--save-result",
      action="store_true",
      help="Specify to save benchmark results to a json file",
  )
  parser.add_argument(
      "--priority",
      type=int,
      default=0,
      help=(
          "Message priority. (currently no business logic implemented, use"
          " default 0)"
      ),
  )
  parser.add_argument(
      "--session-cache",
      type=str,
      default="",
      help=(
          "Location of any pre-cached results. (currently _load_cache_history"
          " not implemented, use default empty str)"
      ),
  )
  parser.add_argument(
      "--save-request-outputs",
      action="store_true",
      help="Specify to store request outputs into a json file",
  )
  parser.add_argument(
      "--request-outputs-file-path",
      type=str,
      default="/tmp/request-outputs.json",
      help=(
          "File path to store request outputs"
      ),
  )

  args = parser.parse_args()
  main(args)
