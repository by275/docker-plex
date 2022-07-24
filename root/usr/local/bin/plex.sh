#!/bin/bash
. /usr/local/bin/variables


function analyze() {
  export LD_LIBRARY_PATH=/usr/lib/plexmediaserver/lib
  export PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR=/config/Library/Application\ Support

  query="SELECT library_section_id, name FROM media_items m LEFT JOIN library_sections l ON l.id = m.library_section_id WHERE library_section_id > 0 GROUP BY name;"
  IFS=$'\n' sections=($("${PLEX_SQLITE}" "${PLEX_DB_FILE}" "$query"))
  for id_name in "${sections[@]}"; do
    IFS="|" read -r -a id_name <<< "$id_name"
    items=($("${PLEX_SQLITE}" "${PLEX_DB_FILE}" "SELECT media_items.metadata_item_id AS metadata_item_id FROM metadata_items, media_items WHERE metadata_items.id = media_items.metadata_item_id AND media_items.width is NULL AND metadata_items.library_section_id = ${id_name[0]}"))
    echo "${id_name[1]}"
    total="${#items[@]}"
    if [ "$total" -eq 0 ]; then echo "Nothing to analyze!"; continue; fi

    count=0

    for item in "${items[@]}"; do
      ((count%${PLEX_ANALYZE_MULTI:-4}==0)) && proc_ids="" && wait
      ((count++))
      proc_ids="${item} ${proc_ids}"
      printf "\r\033[0K[%3d/%3d] Analyzing %s" "${count}" "${total}" "${proc_ids}"
      "${PLEX_SCANNER}" --section ${id_name[0]} --analyze --item ${item} &
    done
    echo ""
  done
}


function stats() {
  echo "$(basename "$PLEX_DB_FILE") ($(stat -c%s "$PLEX_DB_FILE" | numfmt --to=iec --suffix=B --format="%.2f"))"

  echo ""

  # https://github.com/animosity22/homescripts/blob/master/scripts/plex-library-stats
  query="SELECT Id, Items, Library FROM ( SELECT library_section_id AS Id, COUNT(duration) AS Items, name AS Library FROM media_items m LEFT JOIN library_sections l ON l.id = m.library_section_id WHERE library_section_id > 0 GROUP BY name );"
  "$PLEX_SQLITE" -readonly -header -column "$PLEX_DB_FILE" "$query"

  echo ""

  query="SELECT count(*) FROM media_items"
  result=$("$PLEX_SQLITE" -readonly -header -line "$PLEX_DB_FILE" "$query")
  echo "${result:11} files in library"

  echo ""

  query="SELECT count(*) FROM media_parts WHERE deleted_at is not null"
  result=$("$PLEX_SQLITE" -readonly -header -line "$PLEX_DB_FILE" "$query")
  echo "${result:11} media_parts marked as deleted"

  query="SELECT count(*) FROM metadata_items WHERE deleted_at is not null"
  result=$("$PLEX_SQLITE" -readonly -header -line "$PLEX_DB_FILE" "$query")
  echo "${result:11} metadata_items marked as deleted"

  query="SELECT count(*) FROM directories WHERE deleted_at is not null"
  result=$("$PLEX_SQLITE" -readonly -header -line "$PLEX_DB_FILE" "$query")
  echo "${result:11} directories marked as deleted"

  echo ""

  query="SELECT count(*) FROM metadata_items, media_items WHERE metadata_items.id = media_items.metadata_item_id AND metadata_items.metadata_type BETWEEN 1 and 4 AND media_items.width is NULL"
  result=$("$PLEX_SQLITE" -readonly -header -line "$PLEX_DB_FILE" "$query")
  echo "${result:11} metadata_items missing analyzation info"

  query="SELECT count(*) FROM metadata_items meta join media_items media on media.metadata_item_id = meta.id join media_parts part on part.media_item_id = media.id where part.extra_data not like '%deepAnalysisVersion=2%' and meta.metadata_type in (1, 4, 12) and part.file != '';"
  result=$("$PLEX_SQLITE" -readonly -header -line "$PLEX_DB_FILE" "$query")
  echo "${result:11} files missing deep analyzation info"

  query="SELECT COUNT(0) FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id WHERE mi.library_section_id IN ( SELECT id FROM library_sections WHERE section_type = 2 ) AND mp.extra_data NOT LIKE '%intros=%' AND ( SELECT 1 FROM taggings WHERE taggings.metadata_item_id = mi.metadata_item_id AND taggings.text = 'intro' LIMIT 1 ) IS NULL;"
  result=$("$PLEX_SQLITE" -readonly -header -line "$PLEX_DB_FILE" "$query")
  echo "${result:11} not analyzed for intros"
}


function repair() {
  # https://support.plex.tv/articles/repair-a-corrupted-database/
  dbfile="$(basename "$PLEX_DB_FILE")"
  dbback="$dbfile-$(date +%Y-%m-%d)"

  cd "$(dirname "$PLEX_DB_FILE")" && \
  echo ">> Dumping to sql" && \
  "$PLEX_SQLITE" "$dbfile" ".output dump.sql" ".dump" && \
  echo ">> Backing up to '$dbback'" && \
  mv "$dbfile" "$dbback" && \
  echo ">> Importing from sql" && \
  "$PLEX_SQLITE" "$dbfile" ".read dump.sql" && \
  echo "   Successful!" || \
  { echo "   Something went wrong! Restoring..." && mv "$dbback" "$dbfile"; }
  echo ">> Cleaning up" && \
  rm -f \
    dump.sql \
    ${dbfile}-shm \
    ${dbfile}-wal
}

function optimize() {
  if [ -n ${PLEX_TOKEN:-} ]; then
    curl -sX PUT http://localhost:32400/library/optimize?async=1 \
      -H "X-Plex-Token: $PLEX_TOKEN"
  fi
}

# 
# main
# 
if [ "$1" = "repair" ]; then
  repair
elif [ "$1" = "stats" ]; then
  stats
elif [ "$1" = "analyze" ]; then
  analyze
elif [ "$1" = "optimize" ]; then
  optimize
else
  echo "ERROR: Unknown command: $@"
  echo "Usage: plex {analyze,repair,stats,optimize}"
  exit 1
fi
