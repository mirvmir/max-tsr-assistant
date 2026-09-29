"""Read-only catalog boundaries; callers supply the exact release repository."""
from tsr.contracts import OfferPage, Result


def list_candidates(query,catalog_port):
    result=catalog_port.list_candidates(query)
    return result if isinstance(result,Result) else Result.success(result)


def get_exact_snapshots(ids,release_ref,catalog_port):
    result=catalog_port.get_snapshots(ids,release_ref)
    return result if isinstance(result,Result) else Result.success(result)


class DemoCatalog:
    def __init__(self,release):
        self.release=release

    def list_candidates(self,query):
        profile=self.release.profile
        if query.profile_ref.id!=profile.profile_id or query.profile_ref.version!=profile.version:
            return Result.failure('INCOMPATIBLE_PROFILE')
        candidates=tuple(sorted((offer for offer in self.release.offers if offer.variant.category_id==query.category_id),key=lambda item:str(item.snapshot_id)))
        offset=0
        if query.cursor is not None:
            try: offset=int(query.cursor)
            except ValueError: return Result.failure('VALIDATION_ERROR',field_key='cursor')
            if str(offset)!=query.cursor or offset<0 or offset>len(candidates):
                return Result.failure('VALIDATION_ERROR',field_key='cursor')
        items=candidates[offset:offset+query.limit]
        end=offset+len(items)
        return Result.success(OfferPage(items=items,next_cursor=str(end) if end<len(candidates) else None,
            active_catalog_ref=self.release.catalog_ref,data_quality_flags=('catalog.synthetic','catalog.draft')))

    def get_snapshots(self,ids,release_ref):
        if release_ref!=self.release.release_ref:
            return Result.failure('DATA_NOT_READY')
        index={offer.snapshot_id:offer for offer in self.release.offers}
        if any(snapshot_id not in index for snapshot_id in ids):
            return Result.failure('NOT_FOUND')
        return Result.success(tuple(index[snapshot_id] for snapshot_id in ids))
