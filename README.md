# SaveTheFoods · Catalogo nutrizionale

Pagina con colonne nutrizionali e rapporti sempre visibili a sinistra, ricerca, categorie, prezzi e filtri nutrizionali, aggiornata automaticamente da GitHub Actions ogni lunedì, giovedì e sabato alle 04:17 UTC (06:17 in Italia d'estate, 05:17 d'inverno). Gli avvii programmati di GitHub possono subire ritardi.

## Attivazione su GitHub (una sola volta)

1. Crea una repository pubblica vuota; il caricamento iniziale creerà il branch `main`.
2. Carica i file di questa cartella, **inclusa `.github/workflows/aggiorna.yml`**. `.github` è una cartella nascosta su macOS: premi ⌘⇧. per visualizzarla. In alternativa, usa Git o GitHub Desktop per caricare l'intera cartella.
3. In **Settings → Pages → Build and deployment → Source**, scegli **GitHub Actions**.
4. In **Actions → Aggiorna catalogo e pubblica → Run workflow**, avvia la prima esecuzione.
5. Dopo la conclusione del job `deploy`, il link del sito compare in **Settings → Pages** e nel riepilogo del workflow.

Non servono chiavi API, account SaveTheFoods o servizi a pagamento. GitHub Pages deve essere abilitato per questa repository. Il workflow salva anche una nuova versione dei dati: questo conserva lo storico e genera attività nella repository, evitando la normale inattività che può far disattivare i workflow programmati delle repository pubbliche. Eventuali restrizioni dell'account o modifiche future al sito possono comunque richiedere intervento.

Per forzare un aggiornamento usa **Actions → Aggiorna catalogo e pubblica → Run workflow**. Se uno scaricamento o un test fallisce, il job di pubblicazione non parte e la pagina online precedente resta disponibile.

## Uso locale facoltativo

Serve Python 3.9 o superiore. Dalla cartella del progetto:

```sh
python3 -m pip install -r requirements.txt
python3 savethefoods.py
```

Su Windows usa `py` al posto di `python3`. Ogni esecuzione aggiorna i dati e apre la pagina. Per riaprire i dati salvati senza accedere al sito:

```sh
python3 savethefoods.py --offline
```

Puoi anche aprire direttamente `savethefoods_dashboard.html`. Ricerca e filtri funzionano senza connessione; le immagini e i link ai prodotti richiedono Internet. Non sono usate librerie JavaScript esterne.

## Dati e confronti

- Il catalogo pubblico WooCommerce fornisce l'elenco aggiornato; le pagine prodotto forniscono le tabelle nuove e le descrizioni vecchie. Se il catalogo API non è accessibile, lo script prova le sitemap dei prodotti.
- Di norma sono raccolti i prodotti acquistabili; `--includi-esauriti` include anche gli esauriti. Le schede senza valori testuali restano nel catalogo e si possono mostrare togliendo il filtro corrispondente.
- I valori per **100 g** e **100 ml** rimangono distinti. La base non viene indovinata se manca.
- Un trattino indica un dato mancante, mai uno zero. I limiti come `<0,5 g` vengono conservati. Nel CSV principale il limite è nella colonna `*_limite` accanto al valore numerico.
- Proteine/100 kcal e fibre/100 kcal sono rapporti matematici. **Fibre/100 kcal non è un indice di sazietà.** Gli indici non vengono calcolati se i dati necessari contengono disuguaglianze o la scheda presenta incoerenze rilevate.
- Prezzo = prezzo della confezione, non prezzo al kg. Il termine conserva la dicitura del negozio (per esempio «Preferibilmente entro»).
- Le incongruenze del sito vengono segnalate, non corrette inventando valori. Lo script non interpreta le foto delle etichette.
- `data/prodotti_savethefoods.json` e `.csv` sono salvati dallo script automatico; la pagina contiene il timestamp dell'ultimo aggiornamento riuscito.

## Verifica

```sh
python3 -m unittest -v test_savethefoods.py
```

Documentazione: [GitHub Pages con workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages), [workflow programmati](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
