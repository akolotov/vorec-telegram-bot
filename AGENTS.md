# Python Environment

Use the `.venv` virtual environment for all Python development in this project. Install dependencies only into `.venv`, never into the system Python.

# Language

Create all generated artifacts in English unless explicitly asked otherwise.

# Secrets

Never expose private data from environment files in the context window, whether by directly reading files, using commands such as `cat`, `head`, `tail`, or `grep`, or through scripts or any other extraction method.

# Design

Apply YAGNI and KISS: implement only what is needed, using the simplest clear solution.

# Bot Scripts

Run scripts that implement the bot outside the sandbox.
