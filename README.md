# res00edit

Pack a folder into an S2 (SonSilah / District 187) asset archive, or unpack one.

S2 stores assets as a pair of files: `<name>.Res00` (index) + `<name>.ResDt`
(data). This tool builds that pair from a directory tree, and extracts it back.

Requires Python 3 only.

## Usage

```bash
# pack a folder -> Custom.Res00 + Custom.ResDt
python res00.py pack ./myfolder Custom.Res00

# unpack
python res00.py unpack Custom.Res00 Custom.ResDt ./out

# show archive info
python res00.py info Custom.Res00
```

`pack` options:

- `--store` – store files uncompressed (default is zlib).
- `--no-crc` – skip the `CRC\<guid>.crc` manifest (only causes a harmless log
  warning if omitted).
- `--sep {all,backslash,forward}` – path-separator spellings to store. Default
  `all` makes both `\` and `/` asset references resolve; use `backslash` for a
  lean, retail-style archive when all your content uses `\`.

The directory tree is reproduced exactly. Compressible files are zlib-deflated,
incompressible ones are stored.

## Loading the archive in-game

Put the `.Res00` / `.ResDt` pair in the game's data folder and add it to
**`Default.archcfg`** — the game only loads archives listed there. List it
after `Game` so its entries take precedence.

## License

MIT © 2026 leftspace89. See [LICENSE](LICENSE).
