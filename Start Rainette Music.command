#!/usr/bin/env bash
#
# Double-click this file in Finder to start Rainette Music.
#
# A .command file is what macOS opens with Terminal on double-click, which is
# the closest equivalent to double-clicking an .exe on Windows. The terminal
# window it opens stays attached on purpose: if the app fails to start, the
# reason is printed there instead of vanishing.
#
# The first run takes a minute or two while dependencies install. Later runs
# start immediately.
#
cd "$(dirname "${BASH_SOURCE[0]}")"
exec ./run-macos.sh
