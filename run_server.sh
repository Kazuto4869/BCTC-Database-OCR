#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
vllm_bin="${VLLM_BIN:-$HOME/anaconda3/envs/dots_mocr/bin/vllm}"
model_dir="$project_dir/weights/DotsMOCR"

if [[ ! -x "$vllm_bin" ]]; then
    echo "Không tìm thấy vLLM trong môi trường dots_mocr: $vllm_bin" >&2
    exit 1
fi
if [[ ! -f "$model_dir/config.json" || ! -f "$model_dir/model-00001-of-00002.safetensors" || ! -f "$model_dir/model-00002-of-00002.safetensors" ]]; then
    echo "Bộ model local chưa đầy đủ tại $model_dir" >&2
    exit 1
fi

mkdir -p "$project_dir/result"
exec 9> "$project_dir/result/.server.lock"
if ! flock -n 9; then
    echo "Server đã chạy hoặc đang khởi động trong terminal khác. Không bật thêm bản thứ hai."
    exit 0
fi
if command -v curl >/dev/null && curl -fsS --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "Cổng 8000 đã có server hoạt động. Dùng server hiện tại, không bật thêm bản thứ hai."
    exit 0
fi
log_file="$project_dir/result/server_$(date +%Y%m%d_%H%M%S)_$$.log"
echo "Model local: $model_dir"
echo "Log server: $log_file"
echo "Giữ terminal mở. Chờ server sẵn sàng trước khi chạy python main.py."

# FlashInfer sampling requires nvcc; use vLLM's PyTorch sampler instead.
HF_HUB_OFFLINE=1 VLLM_USE_FLASHINFER_SAMPLER=0 "$vllm_bin" serve "$model_dir" \
    --trust-remote-code \
    --served-model-name model \
    --host 127.0.0.1 \
    --port 8000 \
    --gpu-memory-utilization 0.9 \
    --max-model-len 32768 \
    --max-num-seqs 2 \
    --chat-template-content-format string \
    2>&1 | tee "$log_file"
