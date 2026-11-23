"""Container ASGI factory; reads only explicitly injected process environment."""

from .api import create_api
from .config import load_settings
from .environment import apply_environment, pdf_settings, read_environment


def create():
    values = read_environment()
    return create_api(apply_environment(load_settings(), values), mineru_settings=pdf_settings(values))
