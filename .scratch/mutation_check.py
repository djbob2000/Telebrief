import sys
sys.path.insert(0, ".")
import src.processing.cluster_index as ci
import tests.test_cluster_index as t

orig_apply = ci.ClusterIndex.apply
def broken_apply(self, change):  # forget updated centroids after a join
    if change.fragment_count == 1:
        orig_apply(self, change)
ci.ClusterIndex.apply = broken_apply
try:
    t.test_index_path_assigns_like_database_path()
    print("MUTANT SURVIVED")
except AssertionError as exc:
    print("mutant caught:", str(exc)[:120])
finally:
    ci.ClusterIndex.apply = orig_apply

orig_nearest = ci.ClusterIndex.nearest
def no_window(self, query, *, active_since, limit):  # ignore the 72h cutoff
    import datetime as dt
    return orig_nearest(self, query, active_since=dt.datetime(1970,1,1,tzinfo=dt.UTC), limit=limit)
ci.ClusterIndex.nearest = no_window
try:
    t.test_index_path_assigns_like_database_path()
    print("WINDOW MUTANT SURVIVED")
except AssertionError as exc:
    print("window mutant caught")
finally:
    ci.ClusterIndex.nearest = orig_nearest
