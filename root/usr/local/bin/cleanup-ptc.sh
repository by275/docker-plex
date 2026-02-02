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
exec 9>/tmp/ptc_cleanup.lock
flock -n 9 || {
	logf "Already in progress. Aborting!"
	exit 3
}

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
	while IFS=$'\t' read -r -d '' size file; do
		if rm -f -- "$file"; then
			pSize=$((pSize + size))
			pCount=$((pCount + 1))
		fi
	done < <(find "${PTC_ROOT}" -type f -mtime +"$CLEANUP_PTC_AFTER_DAYS" -printf "%s\t%p\0")
fi

# then remove exceed files
if [[ ${CLEANUP_PTC_EXCEED_GB} =~ ^[0-9]+$ ]] && [[ ${CLEANUP_PTC_FREEUP_GB} =~ ^[0-9]+$ ]]; then
	currentSize=$(du -sb --apparent-size "$PTC_ROOT" | cut -f1)
	if [ "${CLEANUP_PTC_EXCEED_GB}" -eq "0" ]; then
		# delete all
		fCount=$(find "$PTC_ROOT" -type f | wc -l)
		find "$PTC_ROOT" -mindepth 1 -delete
		pSize=$((pSize + currentSize))
		pCount=$((pCount + fCount))
	else
		maxSize=$((CLEANUP_PTC_EXCEED_GB * 1024 * 1024 * 1024))
		if [ "$currentSize" -le "$maxSize" ]; then
			logf "Current size $(humanReadableSize "$currentSize") not exceeded ${CLEANUP_PTC_EXCEED_GB}GB"
		else
			freeupSize=$((CLEANUP_PTC_FREEUP_GB * 1024 * 1024 * 1024))
			freeupMin=$((currentSize - maxSize))
			freeupSize=$((freeupSize > freeupMin ? freeupSize : freeupMin))
			freeupTarget=$((pSize + freeupSize))

			while IFS=$'\t' read -r -d '' _ size file; do
				[ "$pSize" -ge "$freeupTarget" ] && break
				if rm -f -- "$file"; then
					pSize=$((pSize + size))
					pCount=$((pCount + 1))
				fi
			done < <(find "$PTC_ROOT" -type f -printf '%T@\t%s\t%p\0' | sort -z -n)
		fi
	fi
fi

# cleanup
find "${PTC_ROOT}" -mindepth 1 -type d -empty -delete

[ "$pCount" -eq 0 ] && exit 0

# final report
logf "###### Total $(humanReadableSize $pSize) of $pCount file(s) cleaned ######"

exit 0
