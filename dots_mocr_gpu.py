"""Run the local dots.mocr weights directly on CUDA, one page at a time."""

from io import BytesIO
from pathlib import Path


class IncompleteOCR(ValueError):
    def __init__(self, raw):
        super().__init__("OCR chưa hoàn chỉnh hoặc đã chạm giới hạn token")
        self.raw = raw


class DirectOCR:
    def __init__(self, model_path):
        import torch
        from transformers import AutoConfig, AutoModelForCausalLM, AutoProcessor

        if not torch.cuda.is_available():
            raise RuntimeError("Không có CUDA. Chạy bằng .venv-dots-gpu/bin/python và kiểm tra nvidia-smi")
        path = str(Path(model_path).resolve())
        config = AutoConfig.from_pretrained(path, trust_remote_code=True, local_files_only=True)
        config.vision_config.attn_implementation = "sdpa"
        self.model = AutoModelForCausalLM.from_pretrained(
            path, config=config, dtype=torch.bfloat16, device_map="cuda:0",
            attn_implementation="sdpa", trust_remote_code=True, local_files_only=True,
        ).eval()
        self.processor = AutoProcessor.from_pretrained(path, trust_remote_code=True, local_files_only=True)
        self.torch = torch
        print(f"dots.mocr đã nạp trên {torch.cuda.get_device_name(0)}", flush=True)

    def recognize(self, png, prompt, max_tokens, max_pixels=786432):
        from PIL import Image
        from qwen_vl_utils import process_vision_info

        with Image.open(BytesIO(png)) as source:
            image = source.convert("RGB")
        inputs = generated = output = None
        memory_failed = False
        try:
            messages = [{"role": "user", "content": [
                {"type": "image", "image": image, "max_pixels": max_pixels},
                {"type": "text", "text": prompt},
            ]}]
            text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            images, videos = process_vision_info(messages)
            inputs = self.processor(text=[text], images=images, videos=videos,
                                    padding=True, return_tensors="pt").to("cuda:0")
            eos = self.model.generation_config.eos_token_id
            eos = [eos] if isinstance(eos, int) else eos
            with self.torch.inference_mode():
                generated = self.model.generate(**inputs, max_new_tokens=max_tokens,
                                                do_sample=False, max_time=None, use_cache=True,
                                                pad_token_id=eos[0] if eos else None)
            output = generated[0, inputs.input_ids.shape[1]:]
            raw = self.processor.decode(output, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            if not len(output) or int(output[-1]) not in (eos or []):
                raise IncompleteOCR(raw)
            return raw
        except self.torch.cuda.OutOfMemoryError:
            memory_failed = True
        finally:
            image.close()
            del inputs, generated, output
            self.torch.cuda.empty_cache()
        if memory_failed:
            raise ValueError("GPU thiếu VRAM khi OCR; thử ảnh nhỏ hơn")


_model = None
_model_path = None


def prepare(backend):
    global _model, _model_path
    path = str(Path(backend["model_path"]).resolve())
    if _model is None or path != _model_path:
        _model = DirectOCR(path)
        _model_path = path
    return _model


def recognize(png, backend):
    return prepare(backend).recognize(png, backend["prompt"], backend["max_tokens"], backend.get("max_pixels", 786432))
