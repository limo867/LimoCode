# Real Model Demonstration

This project includes an offline tool-loop demo. To create a real-model record, use a small disposable workspace rather than this repository.

1. Set `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` in the shell.
2. Place a tiny failing project in a separate workspace.
3. Run the recorder:

```powershell
python scripts/record_real_demo.py "Read the project, repair the failing test, run tests, and summarize the change." --workspace D:\path\to\demo-workspace
```

4. Inspect `examples/real-model-transcript.json` for private filenames, source text, or secrets before retaining it as a course artifact. It is ignored by Git by default.

The recorder saves the final status, final answer, and tool-operation summary. It does not save the API key.
