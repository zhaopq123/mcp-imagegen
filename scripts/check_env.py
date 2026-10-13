"""校验生产 env → Config 落值，并证明 env 通道可覆盖默认值。"""
from config import load_config

RAW = "/etc/mcp-imagegen/env"


def read_env(path=RAW):
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


KEYS = [
    ("REFERENCE_MAX_MB", "reference_max_mb"),
    ("IMAGE_REFERENCE_MAX", "image_reference_max"),
    ("VIDEO_REFERENCE_MAX", "video_reference_max"),
    ("UPLOAD_MAX_MB", "upload_max_mb"),
    ("VIDEO_SUBMIT_QUEUE_WAIT_SEC", "video_submit_queue_wait_sec"),
    ("VIDEO_SUBMIT_RETRY_MIN_SEC", "video_submit_retry_min_sec"),
    ("VIDEO_SUBMIT_RETRY_MAX_SEC", "video_submit_retry_max_sec"),
]


def main():
    env = read_env()
    cfg = load_config(env)
    print("=== 生产 env → Config 实际取值 ===")
    missing = []
    for env_key, attr in KEYS:
        if env_key not in env:
            missing.append(env_key)
            print(f"  {attr:32s} = {getattr(cfg, attr)}  (env 未设,走默认)")
            continue
        print(f"  {attr:32s} = {getattr(cfg, attr)}")
    print(f"\nenv 缺失项: {missing or '无 —— V4 配置已全部落地'}")

    probe = dict(env)
    probe["REFERENCE_MAX_MB"] = "12.5"
    print(f"通道覆盖验证 REFERENCE_MAX_MB=12.5 → "
          f"{load_config(probe).reference_max_mb}")


if __name__ == "__main__":
    main()
