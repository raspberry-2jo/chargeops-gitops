#!/usr/bin/env python3
"""Offline educational gate-count illustration. Not a faithful Argo controller emulator."""
import argparse
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--pattern',required=True,help='Comma-separated S,F,I,E; e.g. S,S,F,S,S')
p.add_argument('--count',type=int,required=True)
p.add_argument('--failure-limit',type=int,required=True)
a=p.parse_args()
measures=[x.strip().upper() for x in a.pattern.split(',')]
if a.count<1 or a.failure_limit<0 or len(measures)!=a.count or any(x not in ('S','F','I','E') for x in measures):
 p.error('pattern must contain exactly count symbols from S,F,I,E; failure limit >= 0')
fails=measures.count('F')
if fails>a.failure_limit:
 result='FAILED (number of failed measurements exceeds failureLimit)'
elif 'E' in measures:
 result='DO NOT PROMOTE: provider errors; actual Argo error policy must be inspected'
elif 'I' in measures:
 result='INCONCLUSIVE: not enough evidence; actual Argo settings determine end state'
else:
 result='PASSED in simplified model; actual controller configuration is authoritative'
print('Pattern:',measures,'failed:',fails,'failureLimit:',a.failure_limit)
print(result)
print('S=condition passed; F=condition failed; I=inconclusive; E=provider/query error.')
print('WARNING: This program is educational. Verify real AnalysisRun status and error/inconclusive limits.')
