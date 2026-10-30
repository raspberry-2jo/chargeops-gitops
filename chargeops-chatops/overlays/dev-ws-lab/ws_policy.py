"""Pure, replayable WS/OCPP candidate policy. 1=pass, -1=fail, 0=not verified.
No cluster writes, client traffic generation or invented reconnect percentiles.
"""
import math

ACTIONS=('Heartbeat','MeterValues','StatusNotification')


class Policy:
    def __init__(self,data=None):
        data=data or {}
        def number(key,default,low,high):
            value=float(data.get(key,default))
            if not math.isfinite(value) or not low<=value<=high:raise ValueError('Invalid policy '+key)
            return value
        self.connection_drop=number('WS_CONNECTION_DROP_PERCENT',2,0,100)
        self.throughput_drop=number('WS_THROUGHPUT_DROP_PERCENT',15,0,100)
        self.error=number('WS_ERROR_PERCENT',1,0,100)
        self.unexpected=number('WS_UNEXPECTED_CLOSE_PERCENT',1,0,100)
        self.heartbeat=number('WS_HEARTBEAT_SUCCESS_PERCENT',99,0,100)
        self.samples=number('WS_MIN_OCPP_SAMPLES',600,1,10000000)
        self.hb_samples=number('WS_MIN_HEARTBEAT_SAMPLES',300,1,10000000)
        self.sessions=number('WS_MIN_SESSION_OPPORTUNITIES',300,1,10000000)
        self.profile_tolerance=number('WS_INPUT_PROFILE_TOLERANCE_PERCENT',10,0,100)
        self.profile_confirmed=data.get('WS_LOAD_PROFILE_CONFIRMED','false')=='true'
        self.profile_id=data.get('WS_LOAD_PROFILE_ID','')
        if self.profile_confirmed and not self.profile_id:raise ValueError('A documented load profile ID is required')
        self.oom_max_age=number('WS_OOM_MAX_SAMPLE_AGE_SECONDS',15,5,60)
        self.max_seconds=number('WS_MAX_OBSERVATION_SECONDS',600,180,900)


def finite(value):return type(value) in (float,int) and math.isfinite(value) and value>=0


def signal(value,limit,reason=None,lower=False):
    if not finite(value):return {'state':0,'value':None,'limit':limit,'reason':reason or 'MISSING_OR_NONFINITE'}
    if reason:return {'state':0,'value':value,'limit':limit,'reason':reason}
    failed=value<limit if lower else value>limit
    if math.isclose(value,limit,rel_tol=1e-12,abs_tol=1e-9):failed=False
    return {'state':-1 if failed else 1,'value':value,'limit':limit,'reason':'THRESHOLD' if failed else 'OK'}


def ratio(numerator,denominator):
    return numerator/denominator if finite(numerator) and finite(denominator) and denominator>0 else None


def drop(current,reference):
    value=ratio(current,reference)
    return max(0,100*(1-value)) if value is not None else None


class Assessment:
    def __init__(self,policy=None):
        self.policy=policy or Policy();self.steady_since=None;self.last_pass=None;self.passes=0

    def evaluate(self,snapshot,now):
        p=self.policy;results={};infra=snapshot.get('infra',{});roles=snapshot.get('roles',{})
        available=infra.get('available');oom=infra.get('oom_events')
        results['available']=signal(available,4,lower=True)
        results['oom']=signal(oom,0)
        health=infra.get('health_ok')
        results['health_db']=signal(1 if health is True else 0 if health is False else None,1,lower=True)
        if infra.get('candidate_restarted'):
            results['candidate_restart']={'state':-1,'value':1,'limit':0,'reason':'CANDIDATE_RESTART'}
        transitioning=infra.get('draining') is not False or infra.get('membership_changed',False)
        if transitioning:self.steady_since=None
        elif self.steady_since is None:self.steady_since=now
        stable_window=self.steady_since is not None and now-self.steady_since>=60
        baseline=snapshot.get('baseline',{})
        reference_connections=baseline.get('connections')
        value=drop(snapshot.get('connections'),reference_connections)
        results['connections']=signal(value,p.connection_drop,None if stable_window else 'DRAIN_OR_SETTLING')
        candidate=roles.get('canary',{});stable=roles.get('stable',{})
        for name,role in roles.items():
            total=role.get('total');errors=role.get('errors');hb_total=role.get('hb_total');hb_success=role.get('hb_success')
            error_rate=ratio(errors,total);hb_rate=ratio(hb_success,hb_total)
            invalid='SAMPLE_FLOOR' if not finite(total) or total<p.samples else None
            if finite(errors) and finite(total) and errors>total:invalid='INCONSISTENT_COUNTERS'
            results[name+'_errors']=signal(100*error_rate if error_rate is not None else None,p.error,invalid)
            invalid_hb='HEARTBEAT_SAMPLE_FLOOR' if not finite(hb_total) or hb_total<p.hb_samples else None
            if finite(hb_success) and finite(hb_total) and hb_success>hb_total:invalid_hb='INCONSISTENT_COUNTERS'
            results[name+'_heartbeat_server']=signal(100*hb_rate if hb_rate is not None else None,p.heartbeat,invalid_hb,lower=True)
            at_start=role.get('connections_at_start');accepted=role.get('accepted');abnormal=role.get('abnormal')
            opportunities=at_start+accepted if finite(at_start) and finite(accepted) else None
            invalid_close='SESSION_SAMPLE_FLOOR' if not finite(opportunities) or opportunities<p.sessions else None
            close_rate=ratio(abnormal,opportunities)
            if finite(abnormal) and finite(opportunities) and abnormal>opportunities:invalid_close='INCONSISTENT_COUNTERS'
            results[name+'_unexpected']=signal(100*close_rate if close_rate is not None else None,p.unexpected,invalid_close)
        if 'canary' not in roles:
            results['canary_data']={'state':0,'value':None,'limit':None,'reason':'NO_CANDIDATE_DATA'}
        reference=stable if snapshot.get('check_stable',True) else baseline
        def normalized(role):
            exposure=role.get('connections')
            return ratio(role.get('success'),exposure*role.get('window',60)) if finite(exposure) else None
        profile_valid=p.profile_confirmed
        for action in ACTIONS:
            c=ratio(candidate.get('received',{}).get(action),candidate.get('connections'))
            s=ratio(reference.get('received',{}).get(action),reference.get('connections'))
            if c is not None:c/=candidate.get('window',60)
            if s is not None:s/=reference.get('window',60)
            if s==0 and c==0:continue  # Same declared absent action, not a missing counter.
            comparison=ratio(c,s)
            if comparison is None or abs(comparison-1)*100>p.profile_tolerance:profile_valid=False
        reference_samples=reference.get('total')
        reason=None
        if not p.profile_confirmed:reason='OFFERED_LOAD_NOT_ATTESTED'
        elif not profile_valid:reason='INPUT_PROFILE_MISMATCH_OR_MISSING'
        elif not finite(reference_samples) or reference_samples<p.samples:reason='REFERENCE_SAMPLE_FLOOR'
        elif not finite(candidate.get('total')) or candidate['total']<p.samples:reason='SAMPLE_FLOOR'
        elif not stable_window:reason='DRAIN_OR_SETTLING'
        results['normalized_throughput']=signal(drop(normalized(candidate),normalized(reference)),p.throughput_drop,reason)
        # Freshness/counter coverage is part of every proof, but a known safety violation remains a failure.
        if snapshot.get('missing'):
            results['coverage']={'state':0,'value':None,'limit':None,'reason':'SOURCE_COVERAGE_MISSING'}
        if 'SCRAPE_COVERAGE' in snapshot.get('missing',[]):
            for name,result in results.items():
                if name not in ('available','oom','health_db','candidate_restart','coverage'):
                    result.update(state=0,reason='DATA_NOT_FRESH')
        states=[r['state'] for r in results.values()]
        if -1 in states:state=-1
        elif 0 in states:state=0
        else:
            if self.last_pass is None or now-self.last_pass>=60:
                self.passes+=1;self.last_pass=now
            state=1 if self.passes>=2 else 0
        if 0 in states:self.passes=0;self.last_pass=None
        return {'state':state,'status':{1:'SUCCESS',-1:'FAILED',0:'UNVERIFIED'}[state],
                'signals':results,'verified_nonoverlapping_windows':self.passes,
                'reconnect_p95':{'mode':'observe-only','value':None,'limit_seconds':10,
                    'reason':'CLIENT_CROSS_POD_HISTOGRAM_NOT_INSTRUMENTED'}}
