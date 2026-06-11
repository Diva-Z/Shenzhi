import os

from master.master_app import main

if __name__ == "__main__":
    main(
        host=os.environ.get("SHENZHI_MASTER_HOST", "127.0.0.1"),
        port=int(os.environ.get("SHENZHI_MASTER_PORT", "9990")),
    )
