# import-audio agent instructions

A shareable agent skill that imports voice recordings from a USB recorder
(built around the DJI Mic 3), transcribes them, and files them where they
belong after the owner confirms. See `README.md`.

- Nothing personal in this repository. Names, folders, projects, keys and
  hostnames belong in each user's own profile (`~/.config/import-audio/`).
  The templates in `skill/import-audio/examples/` use placeholders only.
- The safety rules are the product: never overwrite, never delete a card file
  without a hash-verified receipt or a confirmed discard, never file without the
  owner's confirmation. Changes that loosen them need a test that shows why.
- Tests use synthetic audio and stubbed tools: no devices, network or keys.
  Run `python3 -m unittest discover -s tests -v` (Python 3.11+). CI runs the
  same plus `bash -n` on the scripts.
- Audio files never get committed.
