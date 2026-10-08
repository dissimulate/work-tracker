#!/bin/sh
. "$(dirname "$0")/../lib/demo-tracker.sh"
# DEMO-2's issue in the team's issue tracker, so the brief asks for its fields.
tracker add DEMO-2 link "Issue: [SC-12 Users API](https://issues.example.com/story/12)"
