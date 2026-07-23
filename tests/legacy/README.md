# Frozen aOS 0.1 characterization

`test_v01_characterization.py` is the unchanged 20-case acceptance suite from commit `e920ff2`
(`v0.1.0`). Its SHA-256 digest at the M1 freeze boundary is:

```text
2ffa39a6a461581532c5c6c555d4d9557497b4dece0e001aa8b1df1f0e822622
```

Keep this suite byte-for-byte frozen through the M5 pre-cutover gate. New v2 behavior belongs under
`tests/v2/`; migration fixtures and their provenance belong under `tests/legacy/fixtures/`.
