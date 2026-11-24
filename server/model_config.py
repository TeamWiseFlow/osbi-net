"""SiliconFlow defaults shared by server extraction and local vision."""
import os

SF_BASE_URL = 'https://api.siliconflow.cn/v1'
SELECTED_MODEL = 'deepseek-ai/DeepSeek-V3.2'
PERFORMANCE_MODEL = 'Qwen/Qwen3-32B'
ONTOLOGY_MODEL = 'Qwen/Qwen3.5-27B'
FALLBACK_MODEL = 'deepseek-ai/DeepSeek-V3.2'
VISION_MODEL = 'Qwen/Qwen3-VL-32B-Instruct'
VISION_FALLBACK_MODEL = 'Qwen/Qwen3-VL-30B-A3B-Instruct'
VISION_EXTRA_MODEL = 'zai-org/GLM-4.5V'


def credentials():
    # Separate from the dialogue service's local LLM_API_BASE/LLM_API_KEY.
    return (os.environ.get('WISEFLOW_LLM_API_BASE', SF_BASE_URL),
            os.environ.get('WISEFLOW_LLM_API_KEY') or os.environ.get('SF_KEY', ''))
