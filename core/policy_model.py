"""Policy adapter; business chunking stays separate from shared model transport."""
from . import llm


class SharedPolicyConfig:
    shared_workbench = True

    def __init__(self, original):
        self.chunk_chars = original.chunk_chars
        self.max_chunks = original.max_chunks

    @property
    def enabled(self):
        return llm.effective('LLM_ENABLED', 'true').lower() == 'true'

    @property
    def effective_model(self):
        return llm.effective('LLM_MODEL')


class SharedPolicyClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_error = ''
        self.usage = {'input_tokens': 0, 'output_tokens': 0}
        self.usage_reported = False

    @property
    def available(self):
        return llm.public_config()['llm_ready']

    def chat_json(self, system, user, temperature=0):
        self.last_error = ''
        self.usage = {'input_tokens': 0, 'output_tokens': 0}
        self.usage_reported = False
        try:
            result = llm.complete([{'role': 'system', 'content': system},
                                   {'role': 'user', 'content': user}],
                                  self.cfg.effective_model, temperature)
            self.usage = result.usage
            self.usage_reported = result.usage_reported
            return result.data
        except llm.ModelError as exc:
            self.last_error = str(exc)
            return None
