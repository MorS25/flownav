#!/usr/bin/env python
import rospy
from std_srvs.srv import Empty

import cv2
import numpy as np
import scipy.stats as stats

from common import *
from framebuffer import ROSCamBuffer,VideoBuffer
import framebuffer as fbuf
import scale_matching as smatch

import operator as op
from keyboard_control import KeyboardController,CharMap,KeyMapping
from collections import OrderedDict

import time,sys


TARGET_N_KP = 50
MIN_THRESH = 2000
MAX_THRESH = 2500
LAST_DAY = 10

VERBOSE = 1

gmain_win = "flownav"
gtemplate_win = "Template matching"


def ClusterKeypoints(keypoints,kphist,img):
    if len(keypoints) < 2: return []

    cluster = []
    unclusteredKPs = sorted(keypoints,key=op.attrgetter('pt'))
    while unclusteredKPs:
        clust = [unclusteredKPs.pop(0)]
        kp = clust[0]
        i = 0
        while i < len(unclusteredKPs):
            if overlap(kp,unclusteredKPs[i]):
                clust.append(unclusteredKPs.pop(i))
            else:
                i += 1
        if (len(clust) >= 2): cluster.append(Cluster(clust,img))

    return cluster


def MergeClusters(clusters,img):
    mergedclusters = []
    unmergedclusters = sorted(clusters,key=op.attrgetter('pt'))
    while unmergedclusters:
        clust = unmergedclusters.pop(0).KPs
        i = 0
        while i < len(unmergedclusters):
            if bboverlap(cl,unmergedclusters[i]):
                clust += unmergedclusters.pop(i).KPs
            else:
                i += 1
        mergedclusters.append(Cluster(clust,img))

    return mergedclusters


def uniqid_gen():
    uid = 2 # starts at 2 since default class_id for keypoints can be +/-1
    while(1):
        yield uid
        uid += 1

# ==========================================================
# process options and set up defaults
# ==========================================================
import optparse
import os
 
parser = optparse.OptionParser(usage="flownav.py [options]")
parser.add_option("-b", "--bag", dest="bag", default=None
                  , help="Use feed from a ROS bagged recording. (don't)")

# parser.add_option("-l", "--log", dest="log", default=None
#                   , help="Specify where to output intermediate analysis results. (don't)")

parser.add_option("--threshold", dest="threshold", type=float, default=2000.
                  , help="Set the Hessian threshold for keypoint detection.")

parser.add_option("-m", "--draw-scale-match", dest="showmatches"
                  , action="store_true", default=False
                  , help="Show scale matches for each expanding keypoint.")

parser.add_option("-v", "--verbose", dest="verbose", action="count", default=1
                  , help="Print verbose output to stdout. Multiple v's for more verbosity.")

parser.add_option("-q", "--quiet", dest="quiet", default=False, action='store_true'
                  , help="Quiet all output to stdout. (don't)")

parser.add_option("--no-draw", dest="nodraw"
                  , action="store_true", default=False
                  , help="Don't draw on display image. (true)")

parser.add_option("--video-topic", dest="camtopic", default="/ardrone"
                  , help="Specify the topic for camera feed ('/ardrone').")

parser.add_option("--video-file", dest="video", default=None
                  , help="Load a video file to test.")

parser.add_option("-r","--record-video", dest="record", default=None
                  , help="Record session to video file.")

parser.add_option("--start", dest="start"
                  , type="int", default=0
                  , help="Starting frame number for video file analysis.")

parser.add_option("--stop", dest="stop"
                  , type="int", default=None
                  , help="Stop frame number for video file analysis.")

(opts, args) = parser.parse_args()

VERBOSE = 0 if opts.quiet else opts.verbose
fbuf.VERBOSE = smatch.VERBOSE = VERBOSE

if opts.bag:
    from subprocess import Popen
    DEVNULL = open(os.devnull, 'wb')
    bagp = Popen(["rosbag","play",opts.bag],stdout=DEVNULL,stderr=DEVNULL)

# logger = open(opts.log,'wb') if opts.log else None

video_writer = opts.record

if opts.video:
    try:                opts.video = int(opts.video)
    except ValueError:  pass
    frmbuf = VideoBuffer(opts.video,opts.start,opts.stop,historysize=LAST_DAY+1)
else:
    frmbuf = ROSCamBuffer(opts.camtopic+"/image_raw",historysize=LAST_DAY+1,buffersize=30)

# start the node and control loop
rospy.init_node("flownav", anonymous=False)

kbctrl = None
if opts.camtopic == "/ardrone" and not opts.video:
    kbctrl = KeyboardController(max_speed=0.5,cmd_period=100)
if kbctrl:
    FlatTrim = rospy.ServiceProxy("/ardrone/flattrim",Empty())
    Calibrate = rospy.ServiceProxy("/ardrone/imu_recalib",Empty())

gmain_win = frmbuf.name
cv2.namedWindow(gmain_win, flags=cv2.WINDOW_OPENGL|cv2.WINDOW_NORMAL)
if opts.showmatches: cv2.namedWindow(gtemplate_win, flags=cv2.WINDOW_OPENGL|cv2.WINDOW_NORMAL)
smatch.MAIN_WIN = gmain_win
smatch.TEMPLATE_WIN = gtemplate_win

# ==========================================================
# Print intro output to user
# ==========================================================
if VERBOSE:
    print "Options"
    print "-"*len("Options")
    print "- Subscribed to", (repr(opts.camtopic) if not opts.video else opts.video)
    print "- Hessian threshold set at", repr(opts.threshold)
    print

    if kbctrl:
        print "Keyboard Controls for automated controller"
        print "-"*len("Keyboard Controls for automated controller")
        for k,v in CharMap.items():
            print k.ljust(20),'=',repr(v).ljust(5)
        print

    print "Additional controls"
    print "-"*len("Additional controls")
    print "* Press 'q' at any time to quit"
    print "* Press 'd' at any time to toggle keypoint drawing"
    if opts.video:
        print "* Press 'm' at any time to toggle scale matching drawing"
    if kbctrl:
        print "* Press 'f' while drone is landed and level to perform a flat trim"
        print "* Press 'c' when drone is in a stable hover to recalibrate drone's IMU"

# ==========================================================
# Additional setup before main loop
# ==========================================================
# initialize the feature description and matching methods
bfmatcher = cv2.BFMatcher()
surf_ui = cv2.SURF(hessianThreshold=opts.threshold,extended=True,upright=True)

# mask out a central portion of the image
lastFrame, t_last = frmbuf.grab()
roi = np.zeros(lastFrame.shape,np.uint8)
scrapY, scrapX = lastFrame.shape[0]//8, lastFrame.shape[1]//8
roi[scrapY:-scrapY, scrapX:-scrapX] = True

if opts.record:
    video_writer = cv2.VideoWriter(opts.record, -1, fps=10,frameSize=lastFrame.shape, isColor=False)

idgen = uniqid_gen()
getuniqid = lambda : idgen.next()

# get keypoints and feature descriptors from query image and assign them an id
queryKP, qdesc = surf_ui.detectAndCompute(lastFrame,roi)
for kp in queryKP: kp.class_id = getuniqid()

# helper function
getMatchKPs = lambda x: (queryKP[x.queryIdx],trainKP[x.trainIdx])

# ==========================================================
# main loop
# ==========================================================
errsum = 0
kpHist = OrderedDict()
lastkey = None
while not rospy.is_shutdown():
    currFrame, t_curr = frmbuf.grab()
    t1_loop = time.time() # loop timer
    if not currFrame.size: break
    dispim = cv2.cvtColor(currFrame,cv2.COLOR_GRAY2BGR)

    if VERBOSE > 2: print "Frame time: %8.3f ms" % t_curr

    '''
    First, assign _every_ query keypoint a unique ID
    Note: 1 and -1 are the openCV default class_ids
    '''
    for kp in queryKP:
        if kp.class_id in (1,-1): kp.class_id = getuniqid()

    # # attempt to adaptively threshold
    # err = len(trainKP)-TARGET_N_KP
    # surf_ui.hessianThreshold += 0.3*(err) + 0.05*(errsum+err)
    # if surf_ui.hessianThreshold < MIN_THRESH: surf_ui.hessianThreshold = MIN_THRESH
    # # elif surf_ui.hessianThreshold > MAX_THRESH: surf_ui.hessianThreshold = MAX_THRESH
    # errsum = len(trainKP)-TARGET_N_KP

    '''
    Now, define a one to one mapping to the training keypoints
    '''
    trainKP, tdesc = surf_ui.detectAndCompute(currFrame,roi)

    # Find the best K matches for each keypoint
    if qdesc is None or tdesc is None: matches = []
    else:                              matches = bfmatcher.knnMatch(qdesc,tdesc,k=2)

    matchdist = []
    filteredmatches = []
    for m in matches:                           # Filter out poor matches by
                                                # ratio test , maximum (descriptor) distance
        if (len(m)==2 and m[0].distance >= 0.6*m[1].distance) or m[0].distance >= 0.25:
            continue
        filteredmatches.append(m[0])
        qkp, tkp = getMatchKPs(m[0])
        tkp.class_id = qkp.class_id             # carry over the key point's ID
        matchdist.append(diffKP_L2(qkp,tkp))    # get the match pixel distance
    matches = filteredmatches

    if matchdist:       # Filter out matches with outlier spatial distances
        threshdist = np.mean(stats.trim1(matchdist,0.25)) + 2*np.std(matchdist)
        matches = [m for m,mdist in zip(matches,matchdist) if mdist < threshdist]

    if not opts.nodraw:
        # Draw rectangle around RoI
        cv2.rectangle(dispim,(scrapX,scrapY)
                      ,(currFrame.shape[1]-scrapX,currFrame.shape[0]-scrapY)
                      ,(192,192,192),thickness=2)
        if matches:     # Draw matched keypoints
            qkp, tkp = zip(*map(getMatchKPs,matches))
            cv2.drawKeypoints(dispim, qkp, dispim, color=(0,255,0))
            cv2.drawKeypoints(dispim, tkp, dispim, color=(255,0,0))
            for q,t in zip(qkp,tkp): cv2.line(dispim, inttuple(*q.pt), inttuple(*t.pt), (0,255,0), 1)
        
    '''
    Find an estimate of the scale change for keypoints that are expanding
    Then update the history of expanding keypoints
    '''
    matches = filter(lambda m: trainKP[m.trainIdx].size > queryKP[m.queryIdx].size, matches)
    matches, kpscales = smatch.estimateKeypointExpansion(frmbuf, matches, queryKP, trainKP, kpHist)

    if opts.showmatches:
        lastkey = smatch.drawTemplateMatches(frmbuf, matches, queryKP, trainKP, kpHist, kpscales, dispim=dispim)
    else:
        lastkey = None

    for m,scale in zip(matches,kpscales):
        clsid = trainKP[m.trainIdx].class_id
        if clsid not in kpHist:
            kpHist[clsid] = KeyPointHistory()
            t_A = t_last
        else:                                   
            t_A = kpHist[clsid].timehist[-1][-1]

        # update matched expanding keypoints with accurate scale,
        # latest keypoint and descriptor
        kpHist[clsid].update(trainKP[m.trainIdx],tdesc[m.trainIdx],t_A,t_curr,scale)
        
    # Update the keypoint history for previously expanding keypoint that were
    # not detected/matched in this frame
    detected = sorted(map(op.attrgetter('class_id'),trainKP))

    # get rid of old matches
    kpHist = OrderedDict(filter(lambda kv: kv[1].downdate().age < LAST_DAY, kpHist.items()))

    # keep matches that were missed in this frame
    missed = filter(lambda k: (kpHist[k].age > 0) and (k not in detected), kpHist)
    missed = [(kpHist[k].keypoint,kpHist[clsid].descriptor.reshape(1,-1)) for k in missed]
    if missed:
        missed_kp, missed_desc = zip(*missed)
        trainKP.extend( missed_kp )
        tdesc = missed_desc if tdesc is None else np.r_[tdesc, missed_desc[0]]

    '''
    Finally, perform some simple clustering of adjacent keypoints to
    obtain a more accurate estimate of TTC
    '''
    # cluster keypoints and sort my maximum inter-cluster distance
    ttc_cluster = []
    expandingKPs = filter(lambda x: (x.class_id in kpHist) and (kpHist[x.class_id].age == 0), trainKP)
    cluster = ClusterKeypoints(expandingKPs, kpHist, currFrame)
    for c in cluster:
        tstep = np.array( map(lambda kp: -op.sub(*kpHist[kp.class_id].timehist[-1]), c.KPs) )
        scale = np.array( map(lambda kp: kpHist[kp.class_id].scalehist[-1], c.KPs) )
        ttc_cluster.append(np.median(tstep / scale))

    # if kbctrl and cluster:
    #     c = cluster[0]
    #     x_obs = c.pt[0]
    #     if (x_obs-currFrame.shape[1]//2) < 0: kbctrl.RollRight()
    #     if x_obs >= currFrame.shape[1]//2: kbctrl.RollLeft()

    if not opts.nodraw:
        # Print out drone status to the image
        if kbctrl:
            stat = "BATT=%.2f" % (kbctrl.navdata.batteryPercent)
            cv2.putText(dispim,stat,(10,currFrame.shape[0]-10)
                        ,cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,255))
        elif opts.video and not frmbuf.live:
            stat = "FRAME %4d/%4d" % (frmbuf.cap.get(cv2.CAP_PROP_POS_FRAMES),frmbuf.stop)
            cv2.putText(dispim,stat,(10,currFrame.shape[0]-10)
                        ,cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,255))
        # Draw expanding keypoints with tags
        # expandingKPs = []
        # for m in matches:
        #     qkp = queryKP[m.queryIdx]
        #     tkp = trainKP[m.trainIdx]
        #     scale = kpHist[tkp.class_id].scalehist[-1]
        #     tstep = -np.diff(kpHist[tkp.class_id].timehist[-1])
        #     # tstep = 1
        #     ttc = tstep / scale

        #     kpinfo = "(%d,%.2f,%.3f)" % (kpHist[tkp.class_id].detects,scale,ttc)
        #     cv2.putText(dispim,kpinfo,inttuple(tkp.pt[0]+tkp.size//2,tkp.pt[1]-tkp.size//2)
        #                 ,cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255,255,0))
        #     expandingKPs.append(kpHist[tkp.class_id].keypoint)
        cv2.drawKeypoints(dispim, expandingKPs, dispim, color=(0,0,255)
                          , flags=cv2.DRAW_MATCHES_FLAGS_DRAW_RICH_KEYPOINTS)

        # Draw clusters with tags
        votes = [sum(kpHist[kp.class_id].detects for kp in c.KPs) for c in cluster]
        for c, ttc in zip(cluster,ttc_cluster):
            clustinfo = "(%d,%.2f)" % (len(c.KPs),ttc)
            cv2.putText(dispim,clustinfo,(c.p1[0]-5,c.p1[1])
                        ,cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0,0,255))

            # Draw an arrow denoting the direction to avoid obstacle
            x_obs, y = c.pt
            if (x_obs-(currFrame.shape[1]//2)) < 0: offset = 50
            if x_obs >= (currFrame.shape[1]//2):    offset = -50
            cv2.arrowedLine(dispim, (currFrame.shape[1]//2,currFrame.shape[0] - 50)
                            , (currFrame.shape[1]//2+offset,currFrame.shape[0] - 50)
                            , (0,255,0), 3)

            # draw cluster ranking
            clr = (0,255-sum(kpHist[kp.class_id].detects for kp in c.KPs)*165./max(votes),255)
            cv2.rectangle(dispim,c.p0,c.p1,color=clr,thickness=2)
        
    ''''
    Handle input keyboard events
    '''
    cv2.imshow(gmain_win, dispim)
    if kbctrl:                  # drone keyboard events
       k = cv2.waitKey(1)%256        
       kbctrl.keyPressEvent(k)
       if k == ord('f'):
           try: FlatTrim()
           except rospy.ServiceException, e: print e
       elif k == ord('c'):
           try: Calibrate()
           except rospy.ServiceException, e: print e
    elif opts.video:            # video file controls
       if lastkey in (ord('q'),ord('m')):
           k = lastkey
       elif lastkey is not None:
           k = cv2.waitKey(250)%256
           while k not in map(ord,('\r','s','q',' ','m','b','f')): k = cv2.waitKey(250)%256
       elif not frmbuf.live:
           # limit the loop rate to 10 Hz the hacky way for display purposes
           t = (time.time()-t1_loop)
           k = cv2.waitKey(int(max((0.05-t)*1000,1)))%256
       else:
           k = cv2.waitKey(1)%256
       if k == ord('m'):
           opts.showmatches ^= True
           if opts.showmatches: cv2.namedWindow(gtemplate_win,cv2.WINDOW_OPENGL|cv2.WINDOW_NORMAL)
           else:                cv2.destroyWindow(gtemplate_win)
       while(k == ord('b')):
           frmbuf.seek(-2)
           cv2.imshow(gmain_win,frmbuf.grab()[0])
           k = cv2.waitKey(250)%256
       while(k == ord('f')):
           frmbuf.seek(1)
           cv2.imshow(gmain_win,frmbuf.grab()[0])
           k = cv2.waitKey(250)%256
    if k == ord('d'): opts.nodraw ^= True
    if k == ord('q'): break

    if opts.record: video_writer.write(dispim)

    # shift the buffer of loop data
    lastFrame   = currFrame
    queryKP     = trainKP
    qdesc       = tdesc
    t_last      = t_curr

# clean up
if opts.bag: bagp.kill()
if opts.record: video_writer.release()
if kbctrl: kbctrl.close()
cv2.destroyAllWindows()
frmbuf.close()
