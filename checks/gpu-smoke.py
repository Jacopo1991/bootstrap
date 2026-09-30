"""Run exactly the same GPU assertions on the host and in the container."""
import torch
from transformers import AutoModelForCausalLM

assert torch.cuda.is_available(), 'CUDA is unavailable'
architectures = torch.cuda.get_arch_list()
assert 'sm_120' in architectures, f'Wheel lacks sm_120: {architectures}'
device = torch.device('cuda:0')
assert torch.cuda.get_device_capability(device) == (12, 0), 'Expected the sm_120 card on cuda:0'
torch.manual_seed(0)
a = torch.randn(128, 128, device=device)
b = torch.randn(128, 128, device=device)
product = a @ b
torch.cuda.synchronize()
assert product.device == device and torch.isfinite(product).all()
torch.testing.assert_close(product.cpu(), a.cpu() @ b.cpu(), rtol=1e-3, atol=1e-3)

# Public tiny pretrained GPT-2 fixture: immutable model revision, safetensors,
# no remote code and no authentication. All parameters and inference on GPU.
model = AutoModelForCausalLM.from_pretrained(
    'hf-internal-testing/tiny-random-gpt2',
    revision='71034c5d8bde858ff824298bdedc65515b97d2b9',
    use_safetensors=True,
    trust_remote_code=False,
).to(device).eval()
assert all(parameter.device == device for parameter in model.parameters())
tokens = torch.tensor([[1, 2, 3, 4]], device=device)
with torch.inference_mode():
    logits = model(input_ids=tokens).logits
    output = model.generate(tokens, max_new_tokens=4, do_sample=False,
                            pad_token_id=model.config.eos_token_id)
torch.cuda.synchronize()
assert logits.device == device and torch.isfinite(logits).all()
assert output.device == device and output.shape == (1, 8)
print(f'PASS: {torch.cuda.get_device_name(0)}; sm_120; matmul; tiny GPT-2 GPU inference')
