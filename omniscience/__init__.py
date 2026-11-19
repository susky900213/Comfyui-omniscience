"""Comfyui-omniscience backend package.

The package splits the former monolithic ``omniscience_server``/
``omniscience_core`` modules by responsibility (see DESIGN.md section 4):

- ``paths`` / ``constants`` / ``util`` — shared low-level foundations;
- ``storage`` / ``skills`` / ``context`` / ``checkpointing`` — persistence;
- ``settings`` / ``workspace`` — host configuration and the file sandbox;
- ``prompts`` / ``tools`` — request assembly and the tool execution layer;
- ``model_client`` — the OpenAI-compatible transport;
- ``agent`` — the legacy, global-chat, and persistent agent runtimes;
- ``session`` / ``controller`` / ``routes`` — session lifecycle, HTTP
  controller, and aiohttp route registration.

The top-level ``omniscience_server.py`` and ``omniscience_core.py`` remain as
thin composition-root facades that re-export this package.
"""
