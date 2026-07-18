# §4.3: worker 1 × threads 16 고정 — 단일 프로세스로 취소 유실/스케줄러 이중 실행 제거
bind = "127.0.0.1:5000"        # Apache 우회 차단 (§3.1)
workers = 1
threads = 16
worker_class = "gthread"
timeout = 120
accesslog = "-"
