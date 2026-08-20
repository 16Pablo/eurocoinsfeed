# Euro Coins Official Feed

Feed RSS filtrado para detectar novedades oficiales sobre monedas de euro de circulación.

El proyecto descubre RSS/Atom cuando una web lo ofrece y, cuando no, hace scraping de la
página de entrada. Después filtra por términos numismáticos y de circulación.

## Fuentes

La configuración incluye las fuentes oficiales indicadas por el usuario y EUR-Lex/DOUE.
Mónaco se incluye expresamente: la Comisión publica sus nuevas caras nacionales de 2 euros
en la serie de comunicaciones sobre monedas destinadas a circulación.

## Publicación gratuita

El workflow de GitHub Actions actualiza `public/euro-coins.xml` cada 6 horas.
Puedes servir ese fichero con GitHub Pages y añadir su URL a Feedly.

## Nota

Las páginas de cada organismo cambian con el tiempo. `config/sources.yaml` está separado del
código para poder corregir una URL sin tocar el scraper.
