#!/bin/bash
# Development-location shim. The canonical native start wrapper lives at
# /opt/pasay-pm/bin/start-native-api.sh (system LaunchDaemon runs it). This shim
# forwards here so a manual run from the dev tree uses the same single source.
exec /opt/pasay-pm/bin/start-native-api.sh "$@"
