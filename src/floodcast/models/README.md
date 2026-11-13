# Model integrations — planned for Stage 2

These directories reserve the agreed model-specific structure. They contain no
adapter implementation, default scientific settings or executable Python package.

```text
models/
├── README.md
├── summa/settings/.gitkeep
├── mizuroute/settings/.gitkeep
└── fluxos/settings/.gitkeep
```

- `models/`: Model integrations (planned for Stage 2).
- `<model>/`: Per-model adapter (planned for Stage 2).
- `<model>/settings/`: Model settings (planned for Stage 2).

The empty `.gitkeep` files only preserve the directories in Git. Stage 1 keeps
its existing binary identity checks in `services/local_runner.py`; builds and
execution are unchanged. No test fixtures become production settings.

Stage 2 will introduce the orchestrator, handlers and steps with concrete
model-specific responsibilities. Stage 3 addresses the scientific interconnection
between models. These placeholders implement neither stage.
