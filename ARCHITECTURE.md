# Arhitectura ParkingAI

## Componente

```text
Tauri / React desktop
  ├── pornește serviciul Python local prin `ensure_api`
  ├── autentificare și configurare camere prin REST
  └── dashboard + snapshot/MJPEG prin API

FastAPI (ai-engine/app/parking_service)
  ├── autentificare scrypt + access JWT/refresh rotativ
  ├── SQLite WAL + audit + dovezi JPEG
  ├── Camera Manager (maximum implicit 20)
  └── worker per cameră:
        capture/reconnect thread → coadă maxsize=1 → procesare AI
        reader/stare/zone/ByteTrack per cameră

Android (etapă separată)
  └── contract REST v1 + SSE; fără URL-uri RTSP sau credentiale
```

API-ul rulează în proces separat față de UI. Comanda Tauri `ensure_api` pornește `ai-engine/app/run_api.py`; `POST /api/v1/system/start` pornește modelele și camerele, iar `stop` oprește worker-ele fără să închidă API-ul. La închiderea desktopului, procesul backend pornit de Tauri este oprit. Pentru operare permanentă, serviciul trebuie instalat ulterior ca serviciu separat, nu legat de durata de viață a UI-ului.

## Sursa și procesarea camerelor

- Fiecare cameră are un worker, un thread de captură, un buffer de un singur cadru (cadrele vechi sunt abandonate), starea de reconnect și un ByteTrack separat.
- O eroare de conectare/procesare este izolată la camera respectivă. Backoff-ul începe la 0,5 secunde și crește până la 30 de secunde.
- Detectorul YOLO pentru vehicule, detectorul YOLO de plăcuțe și EasyOCR sunt încărcate o singură dată și folosite printr-un lock comun. Această alegere limitează duplicarea memoriei GPU, dar serializează inferența; debitul pentru 20+ camere nu este garantat și trebuie măsurat pe hardware țintă.
- Tipurile sursă sunt RTSP/RTSPS, HLS/HTTP(S), URL generic și ONVIF. ONVIF interoghează profilurile și URI-ul streamului; nu există încă WS-Discovery multicast ori adaptoare metadata ANPR specifice producătorilor.
- Fluxul video este procesat cu YOLO11n, ByteTrack și modelul YOLO de plăcuțe existent, urmat de EasyOCR. Nu există încă intrare de metadata ANPR din cameră; pipeline-ul actual folosește fallback-ul video+OCR.
- Tracking-ul este local camerei (`cameraId:trackerId`). Identitatea nu este partajată cross-camera.

## Reguli, date și evidențe

Pozițiile vehiculului sunt comparate separat ca previous/current; timpul folosește timestamp UTC al cadrului, nu FPS pentru cronometrul de parcare. Staționarea este evaluată numai în poligoanele configurate pentru camera respectivă și după cinci observații consecutive cu deplasare sub 12 pixeli. Limita este configurabilă per cameră.

SQLite păstrează camerele (inclusiv credentiale criptate), sesiuni de parcare, observații, incidente, evidențe, utilizatori, refresh tokens și audit. Evidențele JPEG sunt fișiere separate, iar baza de date păstrează căile și metadatele lor. API-ul nu expune credentiale; răspunsul obișnuit de camere omite chiar și URL-urile/hosturile private.

## REST, SSE și securitate

Contractul complet și comenzile de pornire sunt în [API.md](./API.md). API-ul are roluri ADMIN/OPERATOR/VIEWER, hash scrypt pentru parole, access JWT de 15 minute, refresh tokenuri rotative, rate limiting pentru login, criptare Fernet a credentialelor camerelor și validare input. Dacă `PARKINGAI_JWT_SECRET`/`PARKINGAI_ENCRYPTION_KEY` nu sunt furnizate prin environment, API-ul generează și persistă aceste secrete în `ai-engine/data/local_secrets.json` la prima pornire (permisiuni restricționate la utilizatorul curent), astfel încât aplicația instalată pornește fără configurare manuală de shell, iar secretele rămân stabile la restart. Similar, dacă nu există niciun utilizator și `PARKINGAI_ADMIN_PASSWORD` nu e setat, este creat automat contul `admin` cu o parolă generată aleator, scrisă o singură dată în `ai-engine/data/FIRST_RUN_ADMIN_PASSWORD.txt`.

Bind-ul implicit este localhost; setarea `PARKINGAI_HOST=0.0.0.0` este necesară numai pentru acces LAN și trebuie însoțită de reguli firewall. Uvicorn nu termină TLS; traficul remote necesită reverse proxy HTTPS.

## Acces la distanță (relay/gateway)

Pentru acces real de pe internet (Android pe 4G/5G, Wi-Fi extern), fără a deschide niciun port de intrare pe routerul primăriei, ParkingAI Desktop poate iniția un tunel WSS outbound către un relay/gateway găzduit separat de operator. Detaliile arhitecturii, modelul de încredere (TOFU + rotație de token, `instance_id` generat aleator, nelegat de IP), ghidul de implementare al relay-ului și endpointurile `/api/v1/remote-access/*` sunt documentate integral în [REMOTE_ACCESS.md](./REMOTE_ACCESS.md). Relay-ul (`ai-engine/app/relay_service`) rulează separat de `parking_service` și nu are acces la baza de date, evidențe sau credențiale de cameră — el doar retransmite trafic HTTPS opac către instanța desktop conectată, care execută cererea în proces față de propria aplicație FastAPI (deci autentificarea JWT/rolurile existente rămân neschimbate).

## Limitări/următorii pași

- Testarea locală nu certifică performanța la 20 de camere și nu validează camere AXIS/Dahua/Hikvision reale.
- OpenCV nu poate raporta fiabil, pe toate build-urile, dacă un stream RTSP inaccesibil are parolă greșită sau host offline.
- Nu sunt încă implementate metadata ANPR vendor-specific, descoperire multicast ONVIF, configurator vizual de poligoane, retenție automată a evidențelor, cursor pagination sau streaming WebRTC.
- Frontend-ul desktop folosește polling pentru dashboard și snapshot; endpointul SSE este disponibil pentru integrarea mobilă.
- Pipeline-ul de procesare curent folosește `parking_service`; scriptul anterior `parking_duration.py` și backup-urile rămân neschimbate pentru referință/testare și nu sunt entry point-ul noului serviciu.
- PWA-ul existent este un prototip separat; nu trebuie folosit ca API de producție sau ca sursă de evenimente.
- Tunelul de acces la distanță este testat local (buclă relay + client de tunel pe loopback, `ai-engine/app/tests/test_remote_access.py`), dar **nu a fost validat împotriva unui VPS public real cu domeniu/TLS reale și un telefon pe 4G/5G**; acest test rămâne de făcut de operator conform [REMOTE_ACCESS.md](./REMOTE_ACCESS.md).
- Relay-ul rutează cereri după `instance_id`; acesta **nu este un backend multi-tenant partajat** — fiecare instanță ParkingAI Desktop (ex. fiecare primărie) rămâne cu propria bază de date, propriile evidențe și proprii utilizatori, izolate complet de celelalte instanțe conectate la același relay.
- Nu există încă un proiect Android nativ în acest workspace; aplicația Android urmează să fie construită separat în Android Studio, consumând contractul din [API.md](./API.md) fie direct (LAN), fie prin relay (internet).
