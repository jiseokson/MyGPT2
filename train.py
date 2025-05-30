import os
import math
import time
import inspect

import torch
from torch.optim.lr_scheduler import LambdaLR

import torch.distributed as dist
from torch.distributed import init_process_group, destroy_process_group
from torch.nn.parallel import DistributedDataParallel as DDP

import wandb

from model import GPTModel, GPTConfig
from fineweb import FineWebDataLoader

ddp = int(os.environ.get("RANK", -1)) != -1

if ddp:
  init_process_group(backend="nccl")

  rank = int(os.environ["RANK"])
  local_rank = int(os.environ["LOCAL_RANK"])
  world_size = int(os.environ["WORLD_SIZE"])

  device = f"cuda:{local_rank}"
  torch.cuda.set_device(device)

  master_process = (local_rank == 0)

else:
  rank = 0
  local_rank = 0
  world_size = 1

  device = "cuda" if torch.cuda.is_available else "cpu"

  master_process = True

device_type = "cuda" if "cuda" in device else "cpu"

total_batch_size = 2**19 # ~0.5M in number of tokens
micro_batch_size = 64
max_input_tokens = 1024
grad_accm_steps = total_batch_size // (micro_batch_size * max_input_tokens * world_size)

weight_decay = 0.1
max_lr = 6e-4
min_lr = max_lr * 0.1
max_steps =  19073
warmup_steps = 715

val_interval = 250
checkpoint_interval = 1000

checkpoint_dir = "checkponit"
os.makedirs(checkpoint_dir, exist_ok=True)

def get_optimizer(model, weight_decay, learning_rate, device_type):
  param_dict = {pn: p for pn, p in model.named_parameters() if p.requires_grad}

  decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
  nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]

  optim_groups = [
    {"params": decay_params, "weight_decay": weight_decay},
    {"params": nodecay_params, "weight_decay": 0.0}
  ]

  fused_available = "fused" in inspect.signature(torch.optim.AdamW).parameters
  use_fused = fused_available and device_type == "cuda"

  optimizer = torch.optim.AdamW(optim_groups, lr=learning_rate, betas=(0.9, 0.95), eps=1e-8, fused=use_fused)

  return optimizer

def get_scheduler(optimizer, max_lr, min_lr, max_steps, warmup_steps):
  def get_lr_factor(step):
    if step < warmup_steps:
      return (step + 1) / warmup_steps
    if step >= max_steps:
      return min_lr / max_lr
    
    decay_ratio = (step - warmup_steps) / (max_steps - warmup_steps)
    coeff = 0.5 * (1 + math.cos(math.pi * decay_ratio))

    return min_lr / max_lr + coeff * (1 - min_lr / max_lr)
  
  return LambdaLR(optimizer, get_lr_factor)

torch.set_float32_matmul_precision("high")

model_config = GPTConfig(vocab_size=50304) # delete
raw_model = GPTModel(model_config).to(device)
torch.compile(raw_model)

model = DDP(raw_model, device_ids=[local_rank]) if ddp else raw_model

optimizer = get_optimizer(raw_model, weight_decay, learning_rate=max_lr, device_type=device_type)
optimizer_scheduler = get_scheduler(optimizer, max_lr, min_lr, max_steps, warmup_steps)
optimizer_scheduler.step()

train_loader = FineWebDataLoader(micro_batch_size, max_input_tokens, local_rank, world_size, "train")
val_loader = FineWebDataLoader(micro_batch_size, max_input_tokens, local_rank, world_size, "val")

if master_process:
  wandb.init(
    project="GPT2",
    config={
      "total_batch_size": total_batch_size,
      "micro_batch_size": micro_batch_size,
      "max_input_tokens": max_input_tokens,
      "grad_accm_steps": grad_accm_steps,

      "weight_decay": weight_decay,
      "max_lr": max_lr,
      "min_lr": min_lr,
      "max_steps": max_steps,
      "warmup_steps": warmup_steps,

      "vocab_size": model_config.vocab_size,
      "n_ctx": model_config.n_ctx,

      "n_layer": model_config.n_layer,
      "n_embd": model_config.n_embd,
      "n_head": model_config.n_head,

      "embd_pdrop": model_config.embd_pdrop,
      "attn_pdrop": model_config.attn_pdrop,
      "resid_pdrop": model_config.resid_pdrop,

      "layer_norm_epsilon": model_config.layer_norm_epsilon,

      "initializer_range": model_config.initializer_range,
    }
  )

for step in range(max_steps):
  last_step = (step == max_steps - 1)

  if step > 0 and (step % val_interval == 0 or last_step):
    model.eval()
    val_loader.reset()

    val_loss_accm = 0
    val_steps = 5

    with torch.inference_mode():
      for _ in range(val_steps):
        x, y = val_loader.next_batch()
        x, y = x.to(device), y.to(device)

        with torch.autocast(device_type=device_type, dtype=torch.bfloat16):
          _, loss = model(x, y)

        loss /= val_steps
        val_loss_accm += loss.detach()

    if ddp:
      dist.all_reduce(val_loss_accm, op=dist.ReduceOp.AVG)

    if master_process:
      wandb.log({
        "step": step,
        "val/loss": val_loss_accm.item(),
      })

      print(f"step: {step:3} | "
            f"val loss: {val_loss_accm.item():8.3f}")

  if master_process:
    start = time.time()

  model.train()
  optimizer.zero_grad()

  loss_accm = 0.0

  for micro_step in range(grad_accm_steps):
    x, y = train_loader.next_batch()
    x, y = x.to(device), y.to(device)

    if ddp:
      model.require_backward_grad_sync = (micro_step == grad_accm_steps - 1)

    with torch.autocast(device_type=device_type, dtype=torch.bfloat16):
      _, loss = model(x, y)

    loss /= grad_accm_steps
    loss_accm += loss.detach()

    loss.backward()

  if ddp:
    dist.all_reduce(loss_accm, op=dist.ReduceOp.AVG)

  norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
  lr = optimizer_scheduler.get_last_lr()[0]

  optimizer.step()
  optimizer_scheduler.step()

  if device_type == "cuda":
    torch.cuda.synchronize() # for logging

  if master_process:
    end = time.time()
    duration = end - start

    proccess_tokens = total_batch_size
    tokens_per_sec = proccess_tokens / duration

    wandb.log({
      "step": step,
      "train/loss": loss_accm.item(),
      "train/lr": lr,
      "train/norm": norm,
    })

    print(f"step: {step:3} | "
          f"loss: {loss_accm.item():8.3f} | "
          f"lr: {lr:.3e} | "
          f"norm: {norm:.3f} | "
          f"duration: {duration*1000:.3f} ms | "
          f"tps: {tokens_per_sec:.3f} tok/s")
    
  if step > 0 and (step % checkpoint_interval == 0 or last_step):
    if master_process:
      checkpoint = {
        "step": step,
        "model_state_dict": raw_model.state_dict(),
      }
      checkpoint_path = f"step{step:05d}.pt"
      
      torch.save(checkpoint, os.path.join(checkpoint_dir, checkpoint_path))
      print(f"{checkpoint_path} saved")

    dist.barrier()

if ddp:
  destroy_process_group()

if master_process:
  wandb.finish()
