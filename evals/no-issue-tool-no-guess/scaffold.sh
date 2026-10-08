#!/bin/sh
. "$(dirname "$0")/../lib/demo-tracker.sh"
# Issue links whose fields are due, in a run with no tool for any issue tracker: the brief asks for them anyway.
tracker add DEMO-2 link "Issue: [SC-12 Users API](https://issues.example.com/story/12)"
tracker add DEMO-3 link "Issue: [SC-13 Users admin page](https://issues.example.com/story/13)"
