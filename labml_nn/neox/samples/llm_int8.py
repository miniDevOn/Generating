from typing import List

import torch
from torch import nn

from labml import monit
from neox.model import LayerGenerator
from neox.utils import get_tokens, print_tokens
from neox.utils.cache import get_cache

# Prompt to complete
PROMPT = 'Einstein was born in the German Empire, but moved to Switzerland in 1895, forsaking his German'


def infer(model: nn.Module, ids: List[int], device: torch.device):
    """
    ### Predict the next token

    :param layers: is the list of layers
    :param ids: are the input token ids
    :param device: is the device of the model
    """

    with torch.no_grad():
        # Get the tokens
        x = torch.tensor(ids)[None, :].to(device)
        # Eval model
        x = model(x)

    # Return predicted token
    return x[0].max(dim=-1)[1].tolist()


def generate():
    """
    ## Generate text
    """

    # Setup [cache](../utils/cache.html) to cache intermediate key/value pairs for faster generation
    cache = get_cache()
    cache.set('use_cache', True)

    # Device
    device = torch.device('cuda:0')

    # Load layers
    layers = list(LayerGenerator(is_clone_layers=True,
                                 dtype=torch.float16,
                                 device=device,
                                 is_llm_int8=True,
                                 ).load())

    model = nn.Sequential(*layers)

    _ = input('Press any key to continue...')

    # Get token ids
    ids = get_tokens(PROMPT)

    # Run the model
    cache.set('state_ids', (None, 1))
    with monit.section('Infer'):
        next_token = infer(model, ids, device)[-1]

    # Append the predicted token
    ids += [next_token]

    # Predict 100 tokens
    for i in range(1, 100):
        # Set the state to use cached activations
        cache.set('state_ids', (i, i + 1))
        # Get next token. Note that we only feed the last token to the model because
        # we cache the key/value pairs of previous tokens.
        with monit.section('Infer'):
            next_token = infer(model, [next_token], device)[-1]
        # Append the predicted token
        ids += [next_token]
        # Print
        print_tokens(ids, [ids])

    _ = input('Press enter to exit')


#
if __name__ == '__main__':
    generate()
