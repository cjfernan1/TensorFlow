# Monitor de precios Éxito / Jumbo

Vigila los precios de los productos que listes en una hoja de Google Sheets. Los consulta en **Éxito** (exito.com) y **Jumbo** (jumbocolombia.com) y te avisa con una **notificación de escritorio de Windows** en dos casos:

- el precio baja bastante (por defecto un 10 % o más respecto a la última consulta o al promedio de 30 días);
- el mismo producto es mucho más barato en una tienda que en la otra. Se compara el **precio por kg, por L o por unidad**, así que funciona aunque las presentaciones sean distintas.

Todo se guarda en `precios.db`, un archivo SQLite local. Ahí quedan el precio base de la primera consulta, el historial, la presentación, el precio por unidad, el código de barras (EAN), la marca y la URL.

## Qué precio se toma

Se guarda siempre el **menor precio que ofrece la página**:

- el precio de oferta, si la tienda tiene un descuento publicado (por ejemplo -10 %);
- el precio con tarjeta, por ejemplo "Paga $3.512" en Jumbo, que es un 20 % menos con las tarjetas de la tienda. Sale de las promociones restringidas a ciertas tarjetas;
- se ignoran las promociones que exigen llevar varias unidades.

También se guardan el precio normal y la condición, así que la notificación dice, por ejemplo, `$3.512 con tarjeta -20% (normal $4.390)`.

## Cómo funciona

Las dos tiendas usan la plataforma VTEX, que tiene una API pública de catálogo:

- **Búsqueda por texto:** `https://<tienda>/api/catalog_system/pub/products/search?ft=<texto>` (los espacios van como `%20`; con `+` la API responde HTTP 400).
- **Plan B de la búsqueda:** si esa ruta falla, se usa `/api/io/_v/api/intelligent-search/product_search/`.
- **Consulta de un producto ya emparejado:** `.../products/search?fq=skuId:<sku>`.
- **Plan B de la consulta:** se lee el JSON-LD de la página del producto.

### Emparejamiento (nombres distintos en cada tienda)

En la hoja escribes el producto como tú lo conoces, por ejemplo `pony malta 1 litro` o `queso mozarella alpina`. El script empareja así:

1. Normaliza el texto: quita tildes y palabras de relleno, y tolera errores de escritura ("mozarella" ≈ "mozzarella").
2. Si escribiste una presentación ("1 litro", "400 g", "x 6 und"), descarta los productos de otro tamaño.
3. Da a cada resultado un puntaje: similitud del nombre (60 %), marca (20 %) y presentación (20 %).
4. Empareja las dos tiendas:
   - primero por **código de barras (EAN)**: si coincide, es el mismo producto aunque el nombre sea distinto;
   - si no hay EAN común, por **misma marca y misma presentación**.
5. Si la coincidencia es clara, la guarda sola. Si es dudosa, por ejemplo "queso mozarella alpina" sin tamaño, te muestra las 5 mejores opciones de cada tienda y eliges con un número.
6. La elección queda guardada. Desde ahí el producto se consulta directamente por su SKU, sin volver a buscar.

## Instalación (Windows)

```powershell
cd price_monitor
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Hoja de Google Sheets

| producto               | umbral_bajada | activo |
|------------------------|---------------|--------|
| pony malta 1 litro     |               | si     |
| queso mozarella alpina | 15            | si     |
| arroz diana 1 kg       |               | no     |

- Solo `producto` es obligatoria.
- `umbral_bajada` (en %) reemplaza el valor por defecto para ese producto.
- `activo = no` pausa el producto.
- Para compartir la hoja: **Compartir → Acceso general → "Cualquier persona con el enlace" (Lector)**.
- Copia el enlace en `config.yaml`, en `sheet_csv_url`. Sirve el enlace normal `.../edit#gid=0`.
- Para probar sin la hoja puedes copiar `productos.ejemplo.csv` a `productos.csv`.

## Uso

```powershell
python monitor.py match        # 1ª vez y cuando agregues productos: empareja y pregunta los dudosos
python monitor.py run --dry-run  # prueba: muestra alertas sin guardar precios ni notificar
python monitor.py run          # consulta, guarda y notifica
python monitor.py report       # tabla: precio actual, base y por unidad en cada tienda
python monitor.py match --reelegir "queso mozarella alpina"   # corregir un emparejamiento
python monitor.py diagnostico "pony malta 1 litro"            # qué responde cada tienda
```

- En la primera ejecución de `run` solo se guardan los precios base, así que no hay alertas de bajada. Las de diferencia entre tiendas sí pueden aparecer.
- Si `run` encuentra un producto nuevo dudoso mientras corre como tarea programada, lo deja **pendiente** y te avisa para que ejecutes `match`.

## Ejecución automática (Programador de tareas)

Este ejemplo lo ejecuta cada 6 horas con `pythonw.exe`, para que no se abra una ventana de consola:

```powershell
schtasks /create /tn "Monitor precios" /sc hourly /mo 6 /f ^
  /tr "\"C:\ruta\price_monitor\.venv\Scripts\pythonw.exe\" \"C:\ruta\price_monitor\monitor.py\" run"
```

Para eliminar la tarea: `schtasks /delete /tn "Monitor precios" /f`.

## Si una tienda bloquea las consultas

Éxito usa Cloudflare, que puede rechazar consultas automáticas (HTTP 403, "protección anti-bots"). Las rechaza siempre desde servidores en la nube; desde un PC de casa a veces sí responde. Cuando pasa:

- el script **no** intenta saltarse esa protección;
- sigue con la otra tienda;
- deja la tienda bloqueada sin emparejar, para reintentarla en la próxima ejecución, y te avisa con una notificación (máximo una vez al día).

Para ver qué responde cada tienda: `python monitor.py diagnostico "pony malta 1 litro"`.

## Configuración (`config.yaml`)

- `thresholds.drop_pct`: % de bajada para alertar (por defecto 10).
- `thresholds.cross_store_pct`: % de diferencia de precio por unidad entre tiendas (por defecto 15).
- `thresholds.cooldown_hours`: horas que deben pasar antes de repetir la misma alerta (por defecto 24).
- `notifications.discounts`: alerta además los descuentos que publica la tienda (precio vs. precio antes).
- `stores.*.sales_channel`: canal de venta VTEX (`sc`). Úsalo si los precios que ves no coinciden con los de tu ciudad.
- `stores.*.base_url`: dominio de la tienda, por si vuelve a cambiar. Jumbo pasó de tiendasjumbo.co a jumbocolombia.com.
- `request_delay_seconds`: pausa entre peticiones a una misma tienda. Mantén un ritmo bajo y respeta los términos de uso de cada tienda.

## Pruebas

```powershell
python -m pytest -q tests
```

Las pruebas no necesitan internet: usan respuestas VTEX de ejemplo. Si una tienda cambia su API, `monitor.log` muestra el error. En ese caso el script prueba automáticamente los planes B (intelligent-search y JSON-LD).
