import requests
import torch
from transformers import pipeline
from argostranslate import translate as _translate


def lab_initialization():
    device = torch.cuda.current_device() if torch.cuda.is_available() else -1
    
    labels = requests.get(
        "https://raw.githubusercontent.com/exorde-labs/TestnetProtocol/main/targets/class_names.json"
    ).json()

    installed_languages = _translate.get_installed_languages()
    return {
        "device": device,
        "labeldict": labels,
        "max_depth": 2,
        "remove_stopwords": False,
        "installed_languages": installed_languages,
        "max_token_count": 500,
    }
