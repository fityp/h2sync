#!/bin/sh
# Run with:  sh "Start H2Sync (Linux).sh"   (cards are often mounted without execute permission)
exec sh "$(dirname "$0")/app/launch.sh" "$@"
