"""Runs only when a real Ollama server with the default models is reachable:
    ollama pull llama3.1:8b && ollama pull nomic-embed-text && pytest tests/test_live_ollama.py"""
import pytest

from ragnarok.config import Config
from ragnarok.llm import OllamaClient

st = OllamaClient(Config()).status()
pytestmark = pytest.mark.skipif(not (st["reachable"] and st.get("chat_model_ok") and st.get("embed_model_ok")),
                                reason="local Ollama with default models not available")


def test_live_end_to_end(tmp_path):
    from ragnarok.runtime import Runtime
    rt = Runtime(Config(data_dir=tmp_path))
    try:
        assert "42" in rt.chat("Use the calculate tool to compute 6*7 and tell me the result.")
        rt.chat("Remember that my favourite language is Python.")
        assert any("python" in f.text.lower() for f in rt.store.facts())
        assert rt.memory.recall("favourite programming language")
    finally:
        rt.close()
