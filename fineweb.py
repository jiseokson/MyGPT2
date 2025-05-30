import os
import multiprocessing as mp

import torch
import tiktoken
import numpy as np
from datasets import load_dataset

from tqdm.auto import tqdm

data_dir = "data"
shard_token_size = int(1e8)
n_procs = os.cpu_count()
chunksize = 128

enc = tiktoken.get_encoding("gpt2")
eot = enc._special_tokens["<|endoftext|>"]

def load_shard(shard_file):
  npt = np.load(shard_file).astype(np.int32)
  return torch.tensor(npt, dtype=torch.long)

class FineWebDataLoader:
  def __init__(self, batch_size, max_input_tokens, proc_rank, num_procs, split="train", root_dir=data_dir):
    self.batch_size = batch_size
    self.max_input_tokens = max_input_tokens
    self.batch_token_size = batch_size * max_input_tokens

    self.proc_rank = proc_rank 
    self.num_procs = num_procs

    self.split = split
    self.root_dir = root_dir

    self.shard_files = sorted(os.path.join(root_dir, file) for file in os.listdir(root_dir) if split in file)

    self.reset()
  
  def reset(self):
    self.shard_idx = 0
    self.all_tokens = load_shard(self.shard_files[self.shard_idx])
    self.position = self.batch_token_size * self.proc_rank

  def next_batch(self):
    start_pos = self.position
    start_all_tokens_len = len(self.all_tokens)

    if self.position + self.batch_token_size + 1 > len(self.all_tokens):
      buf = torch.empty((self.batch_token_size + 1,), dtype=torch.long)

      remainder = len(self.all_tokens) - self.position
      buf[:remainder] = self.all_tokens[-remainder:]

      self._load_next_shard()

      buf[remainder:] = self.all_tokens[ : self.batch_token_size - remainder + 1]

    else:
      buf = self.all_tokens[self.position : self.position + self.batch_token_size + 1]

      if self.position + self.batch_token_size * self.num_procs >= len(self.all_tokens):
        self._load_next_shard()

    self.position = (self.position + self.batch_token_size * self.num_procs) % start_all_tokens_len
    end_pos = self.position

    x = buf[:-1].view(self.batch_size, self.max_input_tokens)
    y = buf[1:].view(self.batch_size, self.max_input_tokens)

    assert (end_pos - start_pos + start_all_tokens_len) % start_all_tokens_len == self.batch_token_size * self.num_procs, \
      f"Actual jump: {(start_pos - end_pos + start_all_tokens_len) % start_all_tokens_len}, Expected: {self.batch_token_size * self.num_procs}"
    
    assert x.dtype == torch.long and y.dtype == torch.long

    return x, y

  def _load_next_shard(self):
    if self.shard_idx == (self.shard_idx + 1) % len(self.shard_files):
      return
    self.shard_idx = (self.shard_idx + 1) % len(self.shard_files)
    self.all_tokens = load_shard(self.shard_files[self.shard_idx])

def tokenize(sample) -> np.array:
  tokens = [eot]
  tokens.extend(enc.encode_ordinary(sample["text"]))
  
  return np.array(tokens, dtype=np.uint16)

def write_file(shard_idx, tokens):
  split = "val" if shard_idx == 0 else "train"
  
  filename = f"{split}_{shard_idx:06d}"
  filepath = os.path.join(data_dir, filename)

  np.save(filepath, tokens)

def download_fineweb():
  print(f"[INFO] Number of CPU processes     : {n_procs}")
  print(f"[INFO] imap chunksize              : {chunksize}")
  
  streaming_dataset = load_dataset("HuggingFaceFW/fineweb-edu", name="sample-10BT", split="train", streaming=True)
  os.makedirs(data_dir, exist_ok=True)

  with mp.Pool(n_procs) as pool:
    shard_idx = 0

    shard_tokens = np.empty((shard_token_size,), dtype=np.uint16)
    token_count = 0

    progress_bar = tqdm(total=shard_token_size, unit="token", desc=f"Shard {shard_idx}")

    for tokens in pool.imap(tokenize, streaming_dataset, chunksize=chunksize):
      if token_count + len(tokens) < shard_token_size:
        shard_tokens[token_count : token_count + len(tokens)] = tokens
        token_count += len(tokens)

        progress_bar.update(len(tokens))

      else:
        if shard_idx == 0:
          progress_bar.refresh()
          progress_bar.close()

          write_file(shard_idx, shard_tokens[:token_count])

          shard_tokens[:len(tokens)] = tokens
          token_count = len(tokens)

        else:
          remainder = shard_token_size - token_count
          shard_tokens[token_count : token_count + remainder] = tokens[:remainder]

          progress_bar.update(remainder)
          progress_bar.refresh()
          progress_bar.close()

          write_file(shard_idx, shard_tokens)

          shard_tokens[:len(tokens) - remainder] = tokens[remainder:]
          token_count = len(tokens) - remainder

        shard_idx += 1
        progress_bar = tqdm(total=shard_token_size, unit="token", desc=f"Shard {shard_idx}")
        progress_bar.update(token_count)

    if token_count > 0:
      write_file(shard_idx, shard_tokens[:token_count])

      progress_bar.close()

if __name__ == "__main__":
  download_fineweb()
