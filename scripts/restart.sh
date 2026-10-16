#!/usr/bin/env bash
set -euo pipefail

./stop.sh
sleep 1
./start.sh
