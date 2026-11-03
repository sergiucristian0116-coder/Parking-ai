# Configurarea camerelor ParkingAI

## Cerințe înainte de conectare

1. Instalează Python 3.12 și dependențele backend din `ai-engine/requirements.txt` în mediul virtual al motorului.
2. Asigură-te că stația ParkingAI poate accesa camerele în rețea și că utilizatorul camerei are numai permisiunile necesare pentru vizualizarea fluxului.
3. Pentru RTSP/HLS, obține URL-ul streamului din documentația/configurația dispozitivului. Formatul căii diferă între modele; ParkingAI nu ghicește endpointul producătorului.

## Secrete și contul admin inițial

La prima pornire (aplicația desktop instalată sau `run_api.py` rulat direct), dacă nu sunt furnizate `PARKINGAI_JWT_SECRET` și `PARKINGAI_ENCRYPTION_KEY` prin environment, ParkingAI generează automat aceste secrete criptografic aleatoare și le persistă în `ai-engine/data/local_secrets.json` (permisiuni restricționate la utilizatorul curent). La repornire, aceleași secrete sunt reîncărcate din acel fișier, astfel încât token-urile JWT existente și credentialele camerelor criptate rămân valide. Nu este necesară nicio setare manuală de variabile de mediu pentru ca aplicația instalată să pornească.

Similar, dacă nu există niciun utilizator în baza de date și `PARKINGAI_ADMIN_PASSWORD` nu este setat, ParkingAI creează automat contul `admin` cu o parolă aleatoare și o scrie o singură dată în `ai-engine/data/FIRST_RUN_ADMIN_PASSWORD.txt`. Autentifică-te cu acele date, creează un cont administrator nominal prin `POST /api/v1/users`, apoi șterge fișierul și rotește/dezactivează contul `admin` generat.

Pentru medii controlate (de exemplu secrete partajate între mai multe stații sau politici corporate de gestionare a secretelor) poți în continuare seta explicit variabilele de mediu înainte de pornire, caz în care fișierele locale de mai sus nu mai sunt folosite/create:

```powershell
$env:PARKINGAI_JWT_SECRET = (& .\ai-engine\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))")
$env:PARKINGAI_ENCRYPTION_KEY = (& .\ai-engine\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
$env:PARKINGAI_ADMIN_PASSWORD = Read-Host "Setează parola inițială ADMIN (minimum 12 caractere)"
```

Indiferent de sursă (fișier local generat sau variabilă de mediu), păstrează `PARKINGAI_ENCRYPTION_KEY`/`local_secrets.json` într-un backup securizat. Fără cheia originală, credentialele camerelor deja salvate nu mai pot fi decriptate. Nu salva parole reale în comenzi salvate, scripturi sau capturi de ecran.

## Adaugă prima cameră

1. Pornește aplicația desktop și autentifică-te ca administrator.
2. Deschide **CAMERE** și apasă **+ ADAUGĂ CAMERA**.
3. Introdu un ID unic (de exemplu `camera-01`) și un nume.
4. Alege `RTSP`, `HLS`, `URL generic` sau `ONVIF`.
5. Pentru RTSP/HLS/URL, introdu URL-ul complet furnizat de administratorul rețelei/camerei. Nu include username-ul sau parola în URL.
6. Pentru ONVIF, introdu host/IP și portul ONVIF; după identificarea profilului de media, completează profilul când este necesar. API-ul poate interoga profilele ONVIF, însă nu face încă discovery multicast automat.
7. Introdu separat username-ul și parola camerei. Credentialele sunt criptate înainte de persistarea în SQLite și nu sunt trimise în răspunsurile obișnuite ale API-ului.
8. Configurează cel puțin un poligon de parcare folosind coordonatele în pixeli ale imaginii camerei, de forma:

   ```json
   [[[100, 100], [600, 100], [600, 400], [100, 400]]]
   ```

   Fiecare listă din exterior reprezintă o zonă. Fără zone configurate, sistemul nu aplică regula de staționare.
9. Setează limita de staționare în secunde. Valoarea implicită este 900 secunde.
10. Apasă **TESTEAZĂ CONEXIUNEA**, apoi **SALVEAZĂ** numai după validarea rezultatului.
11. Apasă **Pornește AI**. Camerele active sunt gestionate independent; fluxurile indisponibile rămân offline și folosesc reconnect cu backoff.

Limita implicită este 20 de camere simultane configurate (`PARKINGAI_MAX_CAMERAS`). Capacitatea de inferență pentru 20 de streamuri depinde de rezoluție, FPS și GPU și trebuie validată cu echipamentul real.

## Rețea și Android

API-ul ascultă implicit numai pe `127.0.0.1:8766`. Pentru acces dintr-un telefon de pe LAN, configurează `PARKINGAI_HOST=0.0.0.0`, permite portul în firewall numai pentru rețeaua autorizată și configurează TLS printr-un reverse proxy. Nu expune HTTP simplu pe internet. Telefonul se conectează la IP-ul serverului ParkingAI și portul API, niciodată direct la RTSP.

`PARKINGAI_PORT` poate fi schimbat; portul preview-ului legacy `8765` rămâne separat. `PARKINGAI_CORS_ORIGINS` acceptă o listă de origini separate prin virgulă.
