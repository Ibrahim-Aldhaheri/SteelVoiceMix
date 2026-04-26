# SteelVoiceMix translations

Compiled `.qm` Qt translation files live here. Generate from `.ts` source:

```bash
# Inside the repo root
pyside6-lupdate gui/*.py -ts gui/translations/steelvoicemix_<locale>.ts
# ...translate strings in the .ts file...
pyside6-lrelease gui/translations/steelvoicemix_<locale>.ts
```

Currently shipped `.qm` files are loaded automatically by `gui/i18n.py`
based on the system locale. Add a new language by adding its `.qm` file
here.
