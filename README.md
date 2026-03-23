# AI Font Generator — Glyphs Plugin

Version 0.620

Glyphs 3 plugin that generates a complete font from a few style reference glyphs using AI. Select some glyphs, hit Generate, and the plugin sends a bitmap image to the server which returns vectorized outlines for all standard Latin glyphs.

## How it works

1. Select glyphs with paths or components as style reference
2. Choose where to put the result: new master, background layer, or overwrite current master
3. The plugin rasterizes your selection and sends it to the server
4. The server generates a full character set via AI and extracts vector outlines
5. Outlines are inserted into your font

Generation runs on a background thread so Glyphs stays responsive.

## What is sent to the server

- A bitmap image of the selected glyphs
- Your Mac username and a Glyphs license identifier
- All data is processed and stored on aringtypeface.com

## Installation

Install via Glyphs Plugin Manager, or copy `AIFontGenerator.glyphsPlugin` to `~/Library/Application Support/Glyphs 3/Plugins/` and restart Glyphs.

## Updates

On each run, the plugin checks `https://aringtypeface.com/fontgen/plugin_version.json` for newer versions. If an update is available, you'll be prompted to install it. Updates are downloaded from `https://aringtypeface.com/fontgen/updates/`.
