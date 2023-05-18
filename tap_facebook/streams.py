import singer
import time
from typing import Any, Sequence, Union, Optional, Dict, cast, List
from datetime import timedelta, datetime, date
from dateutil import parser

from tap_facebook import utils
from facebook_business.exceptions import FacebookRequestError
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.adsinsights import AdsInsights
from facebook_business.adobjects.adreportrun import AdReportRun

logger = singer.get_logger()


class FacebookAdsInsights:
    def __init__(self, config):
        self.config = config
        self.bookmark_key = "date_start"

    def stream(self, account_ids: Sequence[str], state: dict, tap_stream_id: str):

        for account_id in account_ids:
            state = self.process_account(
                account_id, tap_stream_id=tap_stream_id, state=state
            )
        return state

    def process_account(
        self,
        account_id: str,
        tap_stream_id: str,
        state: Dict,
        fields: Optional[Sequence[str]] = None,
    ) -> dict:
        logger.info(f"account_id: {account_id}")
        start_date = self.__get_start(account_id, state, tap_stream_id)

        # override if start_date goes further back then 37 months
        # which the facebook API does not support
        if datetime.utcnow() - start_date > timedelta(days=37 * 30):
            start_date = datetime.utcnow() - timedelta(days=36 * 30)
        else:
            start_date -= timedelta(days=1)

        prev_bookmark = None
        fields = fields or [
            "account_id",
            "account_name",
            "account_currency",
            "ad_id",
            "ad_name",
            "adset_id",
            "adset_name",
            "campaign_id",
            "campaign_name",
            "clicks",
            "ctr",
            "date_start",
            "date_stop",
            "frequency",
            "impressions",
            "reach",
            "social_spend",
            "spend",
            "unique_clicks",
            "unique_ctr",
            "unique_link_clicks_ctr",
            "inline_link_clicks",
            "unique_inline_link_clicks",
        ]
        with singer.record_counter(tap_stream_id) as counter:
            if not start_date:
                raise ValueError("client: start_date is required")

            since = utils.parse_date(start_date)
            until = date.today() - timedelta(days=1)

            logger.info(f"start_date: {since}")
            logger.info(f"end_date: {until}")

            if until - since > timedelta(days=1):
                # for large intervals, the API returns 500
                # handle this by chunking the dates instead
                time_ranges = []

                from_date = since
                while True:
                    to_date = from_date + timedelta(days=1)

                    if to_date > until:
                        break

                    time_ranges.append((from_date, to_date))

                    # add one to to_date to make intervals non-overlapping
                    from_date = to_date + timedelta(days=1)

                if from_date <= until:
                    time_ranges.append((from_date, until))
            else:
                time_ranges = [(since, until)]
            try:
                for (start, stop) in time_ranges:
                    timerange = {"since": str(start), "until": str(stop)}
                    params = {
                        "level": "ad",
                        "limit": 100,
                        "fields": fields,
                        "time_increment": 1,
                        "time_range": timerange,
                    }

                    logger.info(f"account_id: {account_id}")
                    logger.info(f"params: {params}")

                    attempt = 0
                    while True:
                        try:
                            result = self.__run_adreport(account_id, fields, params)

                            ads_insights_result = cast(List[AdsInsights], result.get_result())

                            ads_insight: AdsInsights
                            for ads_insight in ads_insights_result:
                                insight = dict(ads_insight)
                                singer.write_record(tap_stream_id, insight)
                                counter.increment(1)

                                new_bookmark = insight[self.bookmark_key]
                                if not prev_bookmark:
                                    prev_bookmark = new_bookmark

                                if prev_bookmark < new_bookmark:
                                    state = self.__advance_bookmark(
                                        account_id, state, prev_bookmark, tap_stream_id
                                    )
                                    prev_bookmark = new_bookmark

                            break
                        except FacebookRequestError as e:
                            # We frequently encounter this specific error, and have no concrete explanation for
                            # what's actually wrong. It's seemingly temporary, since re-running the tap results
                            # in the job completing just fine.
                            # When this error occurs, technically the async_status of the AdReportRun indicates
                            # job failure, but we have no way of extracting the reasoning for the failure from
                            # the AdReportRun itself. Attempting to get the results of the job is the only real
                            # way of determining if we can retry.
                            if e.api_error_code() == 2601 and e.api_error_subcode() == 1815107 and attempt < 5:
                                logger.warning("encountered unknown, but seemingly temporary async error, retrying in 20s")
                                time.sleep(20)
                                attempt += 1
                                continue

                            raise
            except Exception:
                self.__advance_bookmark(account_id, state, prev_bookmark, tap_stream_id)
                raise
        if not prev_bookmark:
            prev_bookmark = until

        return self.__advance_bookmark(account_id, state, prev_bookmark, tap_stream_id)

    def __get_start(self, account_id, state: dict, tap_stream_id: str) -> datetime:
        default_date = datetime.utcnow() + timedelta(weeks=4)

        config_start_date = self.config.get("start_date")
        if config_start_date:
            default_date = parser.isoparse(config_start_date).replace(tzinfo=None)

        if not state:
            logger.info(f"using 'start_date' from config: {default_date}")
            return default_date

        account_record = singer.get_bookmark(state, tap_stream_id, account_id)
        if not account_record:
            logger.info(f"using 'start_date' from config: {default_date}")
            return default_date

        current_bookmark = account_record.get(self.bookmark_key, None)
        if not current_bookmark:
            logger.info(f"using 'start_date' from config: {default_date}")
            return default_date

        logger.info(f"using 'start_date' from previous state: {current_bookmark}")
        return parser.isoparse(current_bookmark)

    def __run_adreport(
        self,
        account_id: str,
        fields: Optional[Sequence[str]],
        params: Dict[str, Any],
    ) -> AdReportRun:
        async_job = cast(
            AdReportRun,
            AdSet(account_id).get_insights(fields=fields, params=params, is_async=True),
        )

        while True:
            job = cast(AdReportRun, async_job.api_get())

            pct: int = job["async_percent_completion"]
            status: str = job["async_status"]
            job_id = job["id"]

            if status in ["Job Skipped", "Job Failed"]:
                logger.error(f"job<{job_id}>({params}): failed with status: {status}")
                return async_job

            # https://developers.facebook.com/docs/marketing-api/insights/best-practices/#asynchronous
            # both fields need to be set to signify completion
            if status == "Job Completed" and pct == 100:
                job_start_time = datetime.fromtimestamp(job["time_ref"])
                job_completion_time = datetime.fromtimestamp(job["time_completed"])
                duration = job_completion_time - job_start_time

                logger.info(f"job<{job_id}>: finished in {duration.seconds}s")

                return async_job

            # "Job Completed" does not mean that the job is done, which mweans that getting here=e
            # implies that status == "Job Completed" and pct < 100
            if status in [
                "Job Not Started",
                "Job Started",
                "Job Running",
                "Job Completed",
            ]:
                logger.info(f"job<{job_id}>: {status}")

                time.sleep(2)
                continue

    def __advance_bookmark(
        self,
        account_id: str,
        state: dict,
        bookmark: Union[str, datetime, None],
        tap_stream_id: str,
    ):
        if not bookmark:
            singer.write_state(state)
            return state

        if isinstance(bookmark, datetime):
            bookmark_datetime = bookmark
        elif isinstance(bookmark, str):
            bookmark_datetime = parser.isoparse(bookmark)
        elif isinstance(bookmark, date):
            bookmark_datetime = parser.isoparse(bookmark.isoformat())
        else:
            raise ValueError(
                f"bookmark is of type {type(bookmark)} but must be either string or datetime"
            )

        state = singer.write_bookmark(
            state,
            tap_stream_id,
            account_id,
            {self.bookmark_key: bookmark_datetime.isoformat()},
        )

        singer.write_state(state)
        return state
