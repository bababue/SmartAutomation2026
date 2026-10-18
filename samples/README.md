# Public sample logs

Two borehole logs to run the extraction without the confidential challenge dataset. Both are public
records from the borehole portal of the Landesamt für Geologie, Rohstoffe und Bergbau Baden-Württemberg
(LGRB, [bohrungen.lgrb-bw.de](https://bohrungen.lgrb-bw.de)), exported as PDF from the portal.
Source: © LGRB Baden-Württemberg.

| File | Content |
|---|---|
| `BO_7418_00676_Bohrprofil.pdf` | one-sheet log of a geothermal well, 98 m, northing and easting, not yet checked by LGRB |
| `BO_7418_01197_Bohrprofil.pdf` | one-sheet log of a geothermal well, 100 m, northing and easting, checked by LGRB |

```bash
make samples                                          # needs a model key (main README); costs cents
DATA_DIR=data/samples .venv/bin/uvicorn api.main:app --port 8000
```

`make samples` writes pages, cache and database to `data/samples/`, apart from any other run.
