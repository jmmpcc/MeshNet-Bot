#!/usr/bin/env python3
"""Punto de entrada independiente de MeshNet ControlPanel."""

from __future__ import annotations

import argparse
import os

import uvicorn

from web_admin import app


def main() -> None:
    parser = argparse.ArgumentParser(description="Panel de aplicaciones independientes de MeshNet")
    parser.add_argument("--host", default=os.getenv("CONTROLPANEL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CONTROLPANEL_PORT", "8790")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, server_header=False, proxy_headers=True)


if __name__ == "__main__":
    main()
