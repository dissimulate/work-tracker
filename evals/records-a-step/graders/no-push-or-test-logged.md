---
type: regex
target: { source: file, path: .trackers/demo/log.md }
pattern: '^(?![\s\S]*\n- [^\n]*\b(push(ed)?|tests? (pass|passed|green)|passed)\b)[\s\S]*\S'
flags: i
---
