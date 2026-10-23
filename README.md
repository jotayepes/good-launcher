# Good Launcher — lanzador para GOG

Lanzador de juegos de [GOG](https://www.gog.com) para Linux y SteamOS, centrado en que
**las partidas en la nube se sincronicen siempre**, también en el modo juego de la Steam Deck.

> **Aviso:** proyecto independiente y no oficial. No está afiliado, respaldado ni patrocinado
> por GOG sp. z o.o. ni por CD PROJEKT. «GOG» y «GOG GALAXY» son marcas de sus respectivos
> propietarios. gogl usa APIs no documentadas de GOG que pueden cambiar sin aviso.

## Qué hace

- **Biblioteca** de GOG con carátulas, búsqueda y filtros.
- **Instalación propia** (sin depender de otros lanzadores): descarga en paralelo,
  verificación por MD5, reanudación tras cortes, actualizaciones que solo bajan lo que
  cambió, verificación y reparación. Juegos de Windows (Content System v1 y v2) e
  instaladores nativos de Linux.
- **Dependencias de GOG**: DOSBox, ScummVM, etc. van con el juego; Visual C++, DirectX…
  se instalan en el prefijo antes de la primera partida, junto con los scripts de
  instalación de cada juego.
- **Proton-GE gestionado**: descarga sus propias versiones (con verificación SHA-512) en
  su carpeta de datos; no usa ni toca la instalación de Steam.
- **Sincronización de partidas fiable**:
  - compara por contenido (MD5) con un historial a tres bandas, no por fechas;
  - nunca vacía la nube porque una carpeta local esté vacía (p. ej. un prefijo recién creado);
  - copia de seguridad local antes de sobrescribir o borrar nada;
  - un *vigilante* (unidad transitoria de systemd) sube las partidas aunque Steam cierre
    el lanzador al salir del juego, y un timer reintenta las subidas pendientes.
- **Partidas en la nube de toda la cuenta**: espacio usado por juego frente a la cuota,
  y borrado de partidas antiguas.
- **Ajustes por juego**: versión de Proton-GE, idioma, carpetas de partidas, argumentos,
  variables de entorno y comando previo (MangoHud, GameMode…).
- **Extras e instaladores offline**: manuales, bandas sonoras, fondos… y los instaladores
  completos de cada juego (y de sus DLC) para guardarlos, con reanudación y verificación
  por trozos. Se guardan en `~/Descargas/GOG/<juego>` (se puede cambiar en Ajustes).
- **Logros**: en la ficha de cada juego, los conseguidos (con fecha) y los pendientes (con su
  rareza). En los juegos que traen el kit de Galaxy, se consiguen mientras juegas, con un
  aviso en pantalla también en el modo juego. Para eso usa
  [Comet](https://github.com/imLinguin/comet) (Apache-2.0), que se descarga la primera vez
  con su suma SHA-256 fijada. Sin conexión, los logros se guardan y se suben la próxima vez.
- **Tienda de GOG** dentro del lanzador: compras en el checkout de GOG (el lanzador no toca
  datos de pago); avisa si ya tienes un juego y, al comprar, aparece en la biblioteca sin
  reiniciar.
- **Perfil**: tu cuenta, logros conseguidos, juegos al 100 % y lista de deseos con precios
  y rebajas.
- **Integración con Steam**: añade juegos (y el propio lanzador) como «juegos no de Steam»
  con sus carátulas.
- **Control con mando** (cruceta/stick, A, B, LB/RB…), pensado para el modo juego de SteamOS.

## Instalación

### AppImage (recomendada, también en SteamOS)

Un solo archivo con todo incluido (Python y Qt): no instala nada en el sistema, así que
funciona en SteamOS aunque su sistema sea de solo lectura.

```sh
chmod +x gogl-*-x86_64.AppImage
./gogl-*-x86_64.AppImage            # interfaz
./gogl-*-x86_64.AppImage run <id>   # cualquier orden de `gogl`
```

Puede estar en cualquier carpeta y moverse cuando quieras. Steam, el menú de aplicaciones y
las subidas en segundo plano no la llaman por su ruta, sino a un pequeño lanzador
(`~/.local/share/gogl/gogl-launch`) que la AppImage actualiza cada vez que se abre. Si la
mueves y lanzas un juego desde Steam antes de volver a abrirla, el lanzador la busca en tu
carpeta personal y en las tarjetas SD y discos externos (siempre que su nombre contenga «gogl»).

Para construirla: `packaging/build-appimage.sh` (descarga Python, PySide6 y appimagetool
con versiones y sumas SHA-256 fijadas; deja el resultado en `dist/`).

### Desde el código

Hace falta Python 3.11 o superior y, para la interfaz, PySide6 con QtWebEngine.

```sh
git clone <url-del-repositorio>
cd launcher
python3 -m venv .venv
.venv/bin/pip install -e '.[ui]'
.venv/bin/gogl-ui
```

Desde **Ajustes** puedes añadir Good Launcher al menú de aplicaciones y a Steam (para abrirlo
desde el modo juego).

## Línea de comandos

Todo lo de la interfaz está también en el comando `gogl`:

```sh
gogl login                 # iniciar sesión en GOG
gogl install <id>          # instalar un juego de tu biblioteca
gogl run <id>              # sincronizar, jugar y volver a sincronizar
gogl sync <id>             # sincronizar partidas a mano
gogl proton install        # descargar la última versión de Proton-GE
gogl steam add <id>        # añadir un juego a Steam
gogl achievements <id>     # logros conseguidos y pendientes
gogl offline list <id>     # extras e instaladores offline
gogl offline download <id> --extras   # descargar todos los extras
gogl --help                # el resto de órdenes
```

## Dónde guarda las cosas

| Qué | Dónde |
| --- | --- |
| Sesión y ajustes | `~/.config/gogl/` |
| Base de datos, Proton-GE, Comet, prefijos, copias de seguridad, sesión de la tienda | `~/.local/share/gogl/` |
| Extras e instaladores offline (por defecto) | `~/Descargas/GOG/` |
| Registros de cada partida | `~/.local/state/gogl/logs/` |
| Juegos (por defecto) | `~/Games/gogl/` |

## Desarrollo

```sh
.venv/bin/pip install -e '.[ui,dev]'
.venv/bin/python -m pytest
```

## Licencia

[GNU General Public License v3.0 o posterior](LICENSE).
