# Localizer

Pengelola lingkungan pengembangan lokal portabel (Python + Tkinter, tanpa dependensi tambahan).

## Struktur folder

```
Localizer/
├─ localizer.py
├─ config.json          (dibuat/dilengkapi otomatis saat dijalankan)
├─ www/                 (proyek Anda; tiap subfolder = satu situs)
└─ bin/
   ├─ apache/           Apache Lounge: bin/httpd.exe, conf/httpd.conf
   ├─ php/              PHP **Thread Safe** (ada php.exe, php-cgi.exe, php8apache2_4.dll)
   ├─ mysql/            MariaDB: bin/mysqld.exe
   ├─ phpmyadmin/       phpMyAdmin: index.php langsung di folder ini
   ├─ nginx/            (opsional) nginx.exe, conf/nginx.conf
   ├─ redis/            (opsional)
   └─ nodejs/           (opsional, untuk Terminal)
```

### Unduh otomatis (Apache, PHP, MariaDB, phpMyAdmin)

Buka **Pengaturan → Unduh Komponen…** (atau **PHP → Unduh versi PHP…**), centang komponen yang
diinginkan, lalu klik **Unduh**.
Progresnya tampil di log. Versi PHP (bisa lebih dari satu, mis. `8.3 8.4`) dan versi MariaDB
(mis. `11.4`) boleh dikosongkan untuk memakai versi terbaru.

| Komponen   | Lokasi                    | Catatan                                                     |
| ---------- | ------------------------- | ----------------------------------------------------------- |
| Apache     | `bin/apache/`             | versi terbaru dari Apache Lounge                            |
| PHP        | `bin/php/php-<versi>/`    | Thread Safe x64, dicek SHA-256                              |
| MariaDB    | `bin/mysql/`              | LTS stabil terbaru, dicek SHA-256                           |
| phpMyAdmin | `bin/phpmyadmin/`         | versi terbaru, dicek SHA-256                                |

Opsi **Timpa yang sudah ada** tidak menghapus data penting: folder `bin/mysql/data` (database Anda),
`php.ini`, dan `config.inc.php` phpMyAdmin tetap dipertahankan. Layanan yang file-nya akan ditimpa
harus dihentikan dulu.
Bila belum ada PHP aktif, versi terbaru yang dipasang langsung diaktifkan.
Butuh [Visual C++ Redistributable x64](https://aka.ms/vs/17/release/vc_redist.x64.exe). Log memberi peringatan bila belum terpasang.

### Unduh manual

Unduh dan ekstrak ke folder di atas:

| Komponen   | Sumber                                                                  |
| ---------- | ----------------------------------------------------------------------- |
| Apache     | https://www.apachelounge.com/download/ (isi folder `Apache24`)          |
| PHP        | https://windows.php.net/download/ — pilih **VS16/VS17 x64 Thread Safe** |
| MariaDB    | https://mariadb.org/download/ (paket ZIP)                               |
| phpMyAdmin | https://www.phpmyadmin.net/downloads/ (`all-languages.zip`)             |
| Nginx      | https://nginx.org/en/download.html                                      |

Saat aplikasi dibuka, komponen yang belum ada ditampilkan di log, dan titiknya berwarna abu-abu.

### Beberapa versi PHP

Letakkan setiap versi di folder sendiri, misalnya `bin/php/php-8.3/`, `bin/php/php-7.4/`, atau `bin/php-8.2/`
(semua folder `bin/php*` beserta subfoldernya yang berisi `php.exe` akan dikenali).
Pilih versinya di halaman **PHP** (tombol **Aktifkan**). Layanan yang memakai PHP akan di-restart otomatis.
Versi berlabel **NTS** tidak bisa dipakai di Apache, hanya di Nginx (PHP-CGI) dan PHP Server.

## Menjalankan

```
python localizer.py
```

Jalankan sebagai Administrator agar **Sinkron Virtual Host** bisa menulis file `hosts`.

## Membuat exe

```
pip install pyinstaller certifi
pyinstaller --onefile --noconsole --uac-admin --name Localizer localizer.py
```

`certifi` opsional tetapi disarankan: unduhan memercayai sertifikat Windows **dan** bundel certifi,
sehingga tetap berhasil di komputer yang daftar sertifikat akarnya usang. Tanpa certifi, hanya
sertifikat Windows yang dipakai.

Hasilnya `dist/Localizer.exe`. Letakkan exe di folder tujuan. `config.json`, `www/`, dan `bin/` dibuat
atau dibaca di folder yang sama dengan exe, jadi satu folder itu bisa dipindah atau disalin (portabel).
`--uac-admin` membuat exe selalu meminta hak Administrator (dibutuhkan untuk menulis file `hosts`);
hapus opsi itu bila tidak diperlukan.

## Tampilan

Sidebar dengan enam halaman:

| Halaman    | Isi                                                                              |
| ---------- | -------------------------------------------------------------------------------- |
| Dashboard  | Status web server, database, PHP aktif, jumlah situs; akses cepat; aktivitas terbaru |
| Situs      | Daftar situs (nama, URL, jenis Laravel/WordPress/PHP); buka di browser, folder, terminal, VS Code |
| Layanan    | Sakelar on/off per layanan dengan status dan port                                |
| PHP        | Versi terpasang, aktifkan versi lain, edit php.ini, unduh versi baru             |
| Log        | Keluaran semua layanan                                                           |
| Pengaturan | Jalan otomatis, akhiran domain (TLD), folder, unduh komponen                     |

Teks tetap tajam di layar berskala 125%/150%.

## Fitur

- Start/Stop per layanan atau semua sekaligus (`autostart` di config), dengan log langsung
- **Ikon system tray**: tombol X menyembunyikan jendela ke tray (layanan tetap jalan);
  klik ikon untuk membuka, klik kanan untuk Start/Stop All atau Keluar
- **Jalan saat Windows menyala** (centang di aplikasi): Localizer terbuka di tray dan langsung
  menjalankan Start All. Bila dijalankan sebagai Administrator, dipakai Task Scheduler agar hak
  admin tetap terbawa; selain itu kunci Run di registry
- Hanya satu Localizer yang bisa berjalan per folder
- Port di `config.json` dipakai Apache (`Listen`), vhost, dan tombol browser
- Konfigurasi otomatis saat start, tanpa edit manual:
  - **Apache**: `SRVROOT` diarahkan ke folder portabel, `mod_rewrite` diaktifkan, modul PHP dimuat
  - **PHP**: `php.ini` dibuat dari `php.ini-development`, ekstensi di `php_extensions` diaktifkan
  - **MariaDB**: folder data diinisialisasi saat pertama kali (user `root`, password kosong)
  - **Nginx**: situs ditulis ke `conf/localizer-sites.conf`, PHP-CGI dinyalakan otomatis
- **Vhost default**: `http://localhost` menampilkan isi folder `www`
- Virtual host otomatis: folder `www/blog` menjadi `http://blog.test`
  (memakai folder `public/` jika ada; nama folder disesuaikan agar jadi domain valid)
- **phpMyAdmin**: `http://localhost/phpmyadmin`, `config.inc.php` dibuat otomatis
- Terminal dengan PATH berisi PHP, MySQL, Node dari folder `bin`
- Proyek baru: PHP kosong atau Laravel via Composer
- Layanan bisa ditambah/diubah lewat `config.json` (mendukung `env` per layanan)

Semua perubahan pada `httpd.conf` dan `hosts` berada di antara penanda
`# LOCALIZER BEGIN` / `# LOCALIZER END` dan aman dijalankan berulang kali.

## Lisensi

Localizer dirilis di bawah [lisensi MIT](LICENSE).

`Localizer.exe` menyertakan Python (PSF License), Tcl/Tk (BSD-style), dan certifi (MPL-2.0).
Apache, PHP, MariaDB, phpMyAdmin, dan Nginx **tidak** disertakan: komponen tersebut diunduh pengguna
langsung dari situs resminya dan tunduk pada lisensinya masing-masing.
