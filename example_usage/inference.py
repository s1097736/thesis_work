from decorr_mamba.model.mamba import Mamba 
import torch
import torch.nn.functional as F
from transformers import AutoTokenizer
from decorr_mamba.model.decorrelation import DecorrMamba

def generate(model: Mamba,
             tokenizer: AutoTokenizer,
             prompt: str,
             n_tokens_to_gen: int = 50,
             sample: bool = True,
             top_k: int = 40,
             device: str = "cpu"):
    
    model.eval()
    input_ids = tokenizer(prompt, return_tensors='pt').input_ids.to(device)
    
    for token_n in range(n_tokens_to_gen):
        with torch.no_grad():
            indices_to_input = input_ids
            next_token_logits = model(indices_to_input)[:, -1]
        
        probs = F.softmax(next_token_logits, dim=-1)
        (batch, vocab_size) = probs.shape
        
        if top_k is not None:
            (values, indices) = torch.topk(probs, k=top_k)
            probs[probs < values[:, -1, None]] = 0
            probs = probs / probs.sum(axis=1, keepdims=True)
        
        if sample:
            next_indices = torch.multinomial(probs, num_samples=1)
        else:
            next_indices = torch.argmax(probs, dim=-1)[:, None]
        
        input_ids = torch.cat([input_ids, next_indices], dim=1)

    output_completions = [tokenizer.decode(output.tolist()) for output in input_ids][0]
    
    return output_completions

if __name__ == "__main__":

    torch.manual_seed(42)

    device = "cpu"
    model = Mamba.from_pretrained('state-spaces/mamba-370m')
    # model = Mamba.from_pretrained('state-spaces/mamba-1.4b')

    model = DecorrMamba(
        existing_model=model, conv_1d_mode="channel_independent", fuse=True).to(device)
    model.eval()
    model.compute_decorr_losses(False)

    tokenizer = AutoTokenizer.from_pretrained('EleutherAI/gpt-neox-20b', 
        clean_up_tokenization_spaces=True)

    print(generate(model, tokenizer, "def add(x, y): \n", device=device))

