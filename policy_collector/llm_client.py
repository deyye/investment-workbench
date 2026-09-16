"""OpenAI-compatible JSON API; explicit failure reason and actual token usage."""
from __future__ import annotations
import json
import re
import time
from urllib.parse import urlsplit
import requests
from .config import LLMConfig


def _strict_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('模型 JSON 包含重复字段')
        out[key] = value
    return out


class LLMClient:
    def __new__(cls, cfg):
        if getattr(cfg, 'shared_workbench', False):
            from core.policy_model import SharedPolicyClient
            return SharedPolicyClient(cfg)
        return super().__new__(cls)

    def __init__(self,cfg:LLMConfig):
        self.cfg=cfg
        self.base_url=(cfg.base_url or 'https://api.openai.com/v1').rstrip('/')
        self.last_error=''
        self.usage={'input_tokens':0,'output_tokens':0}
        self.usage_reported=False

    @property
    def available(self):
        local = urlsplit(self.base_url).hostname in ('localhost','127.0.0.1','::1')
        return bool(self.cfg.enabled and self.cfg.effective_model and (self.cfg.api_key or local))

    def chat_json(self,system,user,temperature=0.0):
        self.last_error=''
        self.usage={'input_tokens':0,'output_tokens':0}
        self.usage_reported=False
        if not self.available:
            self.last_error='未配置可用的大模型接口'
            return None
        for attempt in range(self.cfg.retries+1):
            try:
                with requests.post(f'{self.base_url}/chat/completions',
                    headers={'Authorization':f'Bearer {self.cfg.api_key}'} if self.cfg.api_key else {},
                    json={'model':self.cfg.effective_model,'temperature':temperature,
                          'response_format':{'type':'json_object'},
                          'messages':[{'role':'system','content':system},{'role':'user','content':user}],
                          **({'enable_thinking':self.cfg.enable_thinking} if self.cfg.enable_thinking is not None else {})},
                    timeout=self.cfg.timeout_seconds) as r:
                    if r.status_code >= 400:
                        self.last_error=f'模型接口 HTTP {r.status_code}'
                        if r.status_code not in (429,500,502,503,504):return None
                        if attempt < self.cfg.retries:
                            time.sleep(min(8,2**attempt));continue
                        return None
                    result=r.json()
                usage=result.get('usage') or {}
                self.usage_reported=all(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens'))
                self.usage['input_tokens'] += max(0, int(usage.get('prompt_tokens',0)))
                self.usage['output_tokens'] += max(0, int(usage.get('completion_tokens',0)))
                choice=result['choices'][0]
                if choice.get('finish_reason') != 'stop':
                    self.last_error='模型输出被截断' if choice.get('finish_reason') == 'length' else '模型未正常完成文本输出'
                    return None
                if choice['message'].get('refusal'):
                    self.last_error='模型拒绝处理';return None
                text=choice['message']['content'].strip()
                text=re.sub(r'^```(?:json)?\s*|\s*```$','',text)
                data=json.loads(text, object_pairs_hook=_strict_object)
                if not isinstance(data,dict):
                    self.last_error='模型输出不是JSON对象';return None
                return data
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
                self.last_error='模型调用或JSON解析失败：'+type(e).__name__
                if attempt < self.cfg.retries:time.sleep(min(8,2**attempt))
        return None
