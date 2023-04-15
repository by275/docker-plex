#!/bin/bash
. /usr/local/bin/variables

# CLEANUP_PTC_AFTER_DAYS=100			# x일 지난 오래된 파일 삭제, 아래 용량 설정보다 우선함, 사용하지 않으려면 주석처리
# CLEANUP_PTC_EXCEED_GB=30			# 이 용량을 초과할 경우 (0이면 아래 옵션 무시하고 언제나 모두 삭제)
# CLEANUP_PTC_FREEUP_GB=20			# 이만큼의 용량을 오래된 파일부터 삭제 (GB 단위)


# logging
logf() {
	echo "$(date "$(printenv DATE_FORMAT)") CLEANU: $1"
}

# Check directory exists
if [ ! -d "${PTC_ROOT}" ]; then
	logf "Directory Not Found: ${PTC_ROOT}"
	exit 1
fi

# If script is already running; abort.
if pidof -o %PPID -x "$(basename "$0")">/dev/null; then
	logf "Already in progress. Aborting!"
	exit 3
fi

# # Check if any files exist, if not exit
# if [ "$(find "${PTC_ROOT}" -type f | wc -l)" = "0" ];then
# 	logf "Nothing to clean. Exiting."
# 	exit 0
# fi

humanReadableSize () {
	numfmt --to=iec "$1" --suffix=B --format="%.2f"
}


# logf "###### Starting Cleanup ######"

pSize=0
pCount=0

# remove old files first
if [[ ${CLEANUP_PTC_AFTER_DAYS} =~ ^[0-9]+$ ]]; then
	while read -r n; do
		# sometimes empty stdin can be redirected by the result of find
		if [ ! -f "$n" ]; then continue; fi

		# # Find the pathname relative to the root of your remote and store filename
		# filename="$(echo "$n" | sed -e "s@${PTC_ROOT}@@")"
		# destpath="$(dirname "$n" | sed -e "s@${PTC_ROOT}@@")"
		# basefile="$(basename "$n")"

		fileSize=$(stat "$n" -c %s)

		if rm -f "$n"; then
			pSize=$((pSize + fileSize))
			pCount=$((pCount + 1))
			# logf "+${CLEANUP_PTC_AFTER_DAYS}D $basefile ($(humanReadableSize $fileSize))"
		fi
	done <<<"$(find "${PTC_ROOT}" -type f -mtime +"$CLEANUP_PTC_AFTER_DAYS" -print)"
fi

# then remove exceed files
if [[ ${CLEANUP_PTC_EXCEED_GB} =~ ^[0-9]+$ ]] && [[ ${CLEANUP_PTC_FREEUP_GB} =~ ^[0-9]+$ ]]; then
	maxSize=$((CLEANUP_PTC_EXCEED_GB * 1000 * 1000 * 1000))
	currentSize="$(du -sb "${PTC_ROOT}" | awk '{print $1}')"
	if [ "${CLEANUP_PTC_EXCEED_GB}" -eq "0" ]; then
		# delete all
		pSize=$((pSize + currentSize))
		pCount=$((pCount + $(find "${PTC_ROOT}" -type f | wc -l)))
		find "${PTC_ROOT}" -mindepth 1 -delete
	elif [ "$maxSize" -gt "$currentSize" ]; then
		logf "Current size of $(humanReadableSize "$currentSize") has not exceeded ${CLEANUP_PTC_EXCEED_GB}GB"
	else
		freeupSize=$((CLEANUP_PTC_FREEUP_GB * 1000 * 1000 * 1000))
		freeupMin=$((currentSize - maxSize))
		freeupSize=$((freeupSize>freeupMin ? freeupSize : freeupMin))
		freeupTotal=$((freeupSize + pSize))

		while read -r n; do
			if [ "$pSize" -gt "$freeupTotal" ]; then
				break
			fi

			# sometimes empty stdin can be redirected by the result of find
			if [ ! -f "$n" ]; then continue; fi

			# Find the pathname relative to the rsoot of your remote and store filename
			# filename="$(echo "$n" | sed -e "s@${PTC_ROOT}@@")"
			# destpath="$(dirname "$n" | sed -e "s@${PTC_ROOT}@@")"
			# basefile="$(basename "$n")"

			fileSize=$(stat "$n" -c %s)

			if rm -f "$n"; then
				pSize=$((pSize + fileSize))
				pCount=$((pCount + 1))
				# logf "+${CLEANUP_PTC_EXCEED_GB}GB $basefile ($(humanReadableSize $fileSize))"
			fi
		done <<<"$(find "${PTC_ROOT}" -type f -print0 | xargs -0 --no-run-if-empty stat --format '%Y :%y %n' | sort -n | cut -d: -f2- | awk '{$1=$2=$3=""; print $0}')"
	fi
fi

# cleanup
find "${PTC_ROOT}" -mindepth 1 -type d -empty -delete

[ "$pCount" -eq 0 ] && exit 0

# final report
logf "###### Total $(humanReadableSize $pSize) of $pCount file(s) cleaned ######"

exit 0
