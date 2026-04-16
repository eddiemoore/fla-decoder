# Research helpers

These scripts were used during the reverse-engineering of the binary FLA
format. They aren't needed at runtime; they're preserved here so anyone
verifying or extending the decoder can re-run the analysis against a
fresh copy of `flash.exe`.

You'll need:

- A copy of `flash.exe` from a Flash Professional 8 install (any era).
- Python 3.10+ with `pefile` and `capstone`:

  ```
  pip install pefile capstone
  ```

## Workflow

```bash
# 1. Find class name strings and CRuntimeClass cross-references.
python research/find_class_refs.py path/to/flash.exe

# 2. Decode every CRuntimeClass struct in the .data section.
python research/decode_runtime_classes.py path/to/flash.exe research/data/runtime_classes.json

# 3. For each CPic*/CMedia* class, locate Serialize via primary vtable slot 4.
python research/find_serialize.py \
    path/to/flash.exe \
    research/data/runtime_classes.json \
    research/data/serialize_vas.json
```

The `data/` directory contains snapshots of `runtime_classes.json` and
`serialize_vas.json` from the original analysis, for reference.

## Obtaining `flash.exe`

You need to source your own copy of `flash.exe` from a Flash Professional 8
install. **Do not commit `flash.exe` (or any other Adobe binary) to this
repository.** It is listed in `.gitignore` for that reason.

The installer historically circulates on archive.org. Reverse engineering
this binary for interoperability with the FLA file format is recognised
as protected activity under DMCA §1201(f) (US) and Article 6 of the EU
Software Directive 2009/24/EC. See [`../NOTICE`](../NOTICE) for the full
legal posture.
