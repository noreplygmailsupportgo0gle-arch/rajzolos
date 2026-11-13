# FIRKA — skribbl.io-szerű játék feltöltése Renderre (ingyenes)

A szerver egyetlen Python fájlból áll (nincs külső függőség), a felület egyetlen HTML fájl.

---

## 1. lépés: GitHub fiók + repó (5 perc)

1. Regisztrálj: https://github.com/signup (ha még nincs fiókod)
2. Jobb felül **+** → **New repository** → név: `firka` → **Private** is jó → **Create**
3. Az üres repo oldalán kattints az **"uploading an existing file"** linkre, és húzd fel ezeket a fájlokat:
   - `firka_server.py`
   - `index.html`
   - `requirements.txt`
   - `words.txt` (ha csináltál saját szólistát)
4. Alul **Commit changes**.

## 2. lépés: Render (ingyenes, bankkártya nélkül)

1. Nyisd meg: https://dashboard.render.com → **Sign in with GitHub**
2. **New +** → **Web Service** → válaszd ki a `firka` repót (Connect)
3. Beállítások:
   - **Language:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python firka_server.py`
   - **Instance Type:** **Free**
4. **Create Web Service** → 1–2 perc build, majd megkapod a címet:
   `https://firka-xxxx.onrender.com`

## 3. lépés: Játék!

- Nyisd meg az URL-t → becenév → **Szoba létrehozása** → a kapott **PIN**-t küldd a haveroknak
- ők ugyanazt az URL-t nyitják, beírják a PIN-t, és már zajlik is a meccs

---

## Tudnivalók az ingyenes csomagról

- **Alvás:** 15 perc tétlenség után a szerver leáll; a következő látogató kb. 1 percet vár,
  utána minden él. (Ha épp játszanak, nem alszik el.)
- **Játékállapot memóriában van:** ha elalszik, a folyamatban lévő meccs elvész — új szobát
  lehet nyitni, nem tragédia.
- **Saját domain:** az ingyenes változaton (a 2026-os Render-dokumentáció szerint) nem
  csatlakoztatható egyéni domain. Addig is a domainjeidről beállítható átirányítás (redirect)
  az onrender.com címre a Rackhost paneljén, vagy később $7/hó-ért mindig élő, domainös
  változat.

## Szólista cseréje

`words.txt` a repo gyökerében: soronként egy szó, `#`-tel kommentelhető sor.
Módosítás után Render → **Manual Deploy** → újraindul az új listával.

## Helyi futtatás (saját gépen teszthez)

```
python firka_server.py 8080
```
majd böngészőben: http://localhost:8080
