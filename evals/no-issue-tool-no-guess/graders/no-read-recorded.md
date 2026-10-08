---
type: regex
target: { source: file, path: .trackers/demo/.state.json }
pattern: '^(?![\s\S]*"read": \{[^}]*"DEMO-[23]")[\s\S]*\S'
---
