#!/usr/bin/env python

# ---------------------------------------------------------------------------
# Licensing Information: You are free to use or extend these projects for
# education or reserach purposes provided that (1) you retain this notice
# and (2) you provide clear attribution to UC Berkeley, including a link
# to http://barc-project.com
#
# Attibution Information: The barc project ROS code-base was developed
# at UC Berkeley in the Model Predictive Control (MPC) lab by Jon Gonzales
# (jon.gonzales@berkeley.edu). The cloud services integation with ROS was developed
# by Kiet Lam  (kiet.lam@berkeley.edu). The web-server app Dator was
# based on an open source project by Bruce Wootton
# ---------------------------------------------------------------------------

import rospy
from Localization_helpers import Localization
from barc.msg import ECU, pos_info, Vel_est
from sensor_msgs.msg import Imu
from marvelmind_nav.msg import hedge_pos
from std_msgs.msg import Header
from numpy import eye, array, zeros, diag, unwrap, tan, cos, sin, vstack, linalg, append
from numpy import ones, polyval, delete, size
from observers import ekf
from system_models import f_SensorKinematicModel, h_SensorKinematicModel
from tf import transformations
import math

# ***_meas are values that are used by the Kalman filters
# ***_raw are raw values coming from the sensors

class StateEst(object):
    """This class contains all variables that are read from the sensors and then passed to
    the Kalman filter."""
    # input variables
    cmd_servo = 0.0
    cmd_motor = 0.0
    cmd_t = 0.0

    # IMU
    yaw_prev = 0.0
    yaw0 = 0.0            # yaw at t = 0
    yaw_meas = 0.0
    psiDot_meas = 0.0
    a_x_meas = 0.0
    a_y_meas = 0.0
    imu_updated = False
    att = (0.0,0.0,0.0)               # attitude

    # Velocity
    vel_meas = 0.0
    vel_updated = False
    vel_prev = 0.0
    vel_count = 0.0               # this counts how often the same vel measurement has been received

    # GPS
    x_meas = 0.0
    y_meas = 0.0
    gps_updated = False
    x_hist = zeros(15)
    y_hist = zeros(15)
    t_gps = zeros(15)
    c_X = array([0,0,0])
    c_Y = array([0,0,0])

    # Estimator data
    x_est = 0.0
    y_est = 0.0

    # General variables
    t0 = 0.0                # Time when the estimator was started
    running = False         # bool if the car is driving

    def __init__(self):
        self.x_meas = 0

    # ecu command update
    def ecu_callback(self, data):
        self.cmd_motor = data.motor        # input motor force [N]
        self.cmd_servo = data.servo        # input steering angle [rad]
        if not self.running:               # set 'running' to True once the first command is received -> here yaw is going to be set to zero
            self.running = True

    # ultrasound gps data
    def gps_callback(self, data):
        """This function is called when a new GPS signal is received."""
        # units: [rad] and [rad/s]
        t_now = rospy.get_rostime().to_sec()-self.t0
        t_msg = data.header.stamp.to_sec()-self.t0
        # if abs(t_now - t_msg) > 0.1:
        #    print "GPS: Bad synchronization - dt = %f"%(t_now-t_msg)
        self.x_meas = data.x_m
        self.y_meas = data.y_m
        dist = (self.x_est-data.x_m)**2 + (self.y_est-data.y_m)**2
        if dist < 1.0:
            self.x_hist = append(self.x_hist, data.x_m)
            self.y_hist = append(self.y_hist, data.y_m)
            self.t_gps = append(self.t_gps, t_msg)
        # self.x_hist = delete(self.x_hist,0)
        # self.y_hist = delete(self.y_hist,0)
        # self.t_gps  = delete(self.t_gps,0)
        self.x_hist = self.x_hist[self.t_gps > t_now-1.0]
        self.y_hist = self.y_hist[self.t_gps > t_now-1.0]
        self.t_gps = self.t_gps[self.t_gps > t_now-1.0]
        sz = size(self.t_gps, 0)
        if sz > 4:
            t_matrix = vstack([self.t_gps**2, self.t_gps, ones(sz)]).T
            self.c_X = linalg.lstsq(t_matrix, self.x_hist)[0]
            self.c_Y = linalg.lstsq(t_matrix, self.y_hist)[0]
        self.gps_updated = True

    # imu measurement update
    def imu_callback(self, data):
        # units: [rad] and [rad/s]
        current_t = rospy.get_rostime().to_sec()

        # get orientation from quaternion data, and convert to roll, pitch, yaw
        # extract angular velocity and linear acceleration data
        ori = data.orientation
        quaternion = (ori.x, ori.y, ori.z, ori.w)
        (roll_raw, pitch_raw, yaw_raw) = transformations.euler_from_quaternion(quaternion)
        # yaw_meas is element of [-pi,pi]
        yaw = unwrap([self.yaw_prev, yaw_raw])[1]       # get smooth yaw (from beginning)
        self.yaw_prev = self.yaw_meas                   # and always use raw measured yaw for unwrapping
        # from this point on 'yaw' will be definitely unwrapped (smooth)!
        if not self.running:
            self.yaw0 = yaw              # set yaw0 to current yaw
            self.yaw_meas = 0                 # and current yaw to zero
        else:
            self.yaw_meas = yaw - self.yaw0

        # extract angular velocity and linear acceleration data
        #w_x = data.angular_velocity.x
        #w_y = data.angular_velocity.y
        w_z = data.angular_velocity.z
        a_x = data.linear_acceleration.x
        a_y = data.linear_acceleration.y
        a_z = data.linear_acceleration.z

        self.psiDot_meas = w_z
        # The next two lines 'project' the measured linear accelerations to a horizontal plane
        self.a_x_meas = cos(-pitch_raw)*a_x + sin(-pitch_raw)*sin(-roll_raw)*a_y - sin(-pitch_raw)*cos(-roll_raw)*a_z
        self.a_y_meas = cos(-roll_raw)*a_y + sin(-roll_raw)*a_z
        #self.a_x_meas = a_x
        #self.a_y_meas = a_y
        self.att = (roll_raw,pitch_raw,yaw_raw)
        self.imu_updated = True

    def vel_est_callback(self, data):
        #self.vel_meas = (data.vel_fl+data.vel_fr)/2.0#data.vel_est
        if data.vel_est != self.vel_prev:
            self.vel_meas = data.vel_est
            self.vel_updated = True
            self.vel_prev = data.vel_est
            self.vel_count = 0
        else:
            self.vel_count = self.vel_count + 1
            if self.vel_count > 10:     # if 10 times in a row the same measurement
                self.vel_meas = 0       # set velocity measurement to zero
                self.vel_updated = True

# state estimation node
def state_estimation():
    se = StateEst()
    # initialize node
    rospy.init_node('state_estimation', anonymous=True)

    # topic subscriptions / publications
    rospy.Subscriber('imu/data', Imu, se.imu_callback)
    rospy.Subscriber('vel_est', Vel_est, se.vel_est_callback)
    rospy.Subscriber('ecu', ECU, se.ecu_callback)
    rospy.Subscriber('hedge_pos', hedge_pos, se.gps_callback, queue_size=1)
    state_pub_pos = rospy.Publisher('pos_info', pos_info, queue_size=1)

    # get vehicle dimension parameters
    L_f = rospy.get_param("L_a")       # distance from CoG to front axel
    L_r = rospy.get_param("L_b")       # distance from CoG to rear axel
    vhMdl = (L_f, L_r)

    # set node rate
    loop_rate = 50
    dt = 1.0 / loop_rate
    rate = rospy.Rate(loop_rate)
    se.t0 = rospy.get_rostime().to_sec()                    # set initial time

    z_EKF = zeros(14)                                       # x, y, psi, v, psi_drift
    P = eye(14)                                             # initial dynamics coveriance matrix

    qa = 1000
    qp = 1000
    #         x, y, vx, vy, ax, ay, psi, psidot, psidrift, x, y, psi, v
    #Q = diag([1/20*dt**5*qa,1/20*dt**5*qa,1/3*dt**3*qa,1/3*dt**3*qa,dt*qa,dt*qa,1/3*dt**3*qp,dt*qp,0.01, 0.01,0.01,1.0,1.0,0.1])
    #R = diag([0.5,0.5,0.5,0.1,10.0,1.0,1.0,     5.0,5.0,0.1,0.5, 1.0, 1.0])

    Q = diag([1/20*dt**5*qa,1/20*dt**5*qa,1/3*dt**3*qa,1/3*dt**3*qa,dt*qa,dt*qa,1/3*dt**3*qp,dt*qp,0.1, 0.01,0.01,1.0,1.0,0.1])
    R = diag([5.0,5.0,1.0,10.0,100.0,1000.0,1000.0,     5.0,5.0,10.0,1.0, 10.0,10.0])
    #         x,y,v,psi,psiDot,a_x,a_y, x, y, psi, v

    # Set up track parameters
    l = Localization()
    l.create_track()
    l.prepare_trajectory(0.06)

    d_f_hist = [0]*10       # assuming that we are running at 50Hz, array of 10 means 0.2s lag
    d_f_lp = 0
    a_lp = 0

    t_now = 0

    # Estimation variables
    (x_est, y_est, a_x_est, a_y_est) = [0]*4
    bta = 0
    v_est = 0
    psi_est = 0

    est_counter = 0

    while not rospy.is_shutdown():
        t_now = rospy.get_rostime().to_sec()-se.t0
        # make R values dependent on current measurement (robust against outliers)
        # sq_gps_dist = (se.x_meas-x_est)**2 + (se.y_meas-y_est)**2
        # if se.gps_updated and sq_gps_dist < 0.8:      # if there's a new gps value:
        #     R[0,0] = 1.0
        #     R[1,1] = 1.0
        # else:
        #     # otherwise just extrapolate measurements:
        #     #se.x_meas = x_est + dt*(v_est*cos(psi_est+bta))
        #     #se.y_meas = y_est + dt*(v_est*sin(psi_est+bta))
        #     R[0,0] = 10.0
        #     R[1,1] = 10.0
        # if se.imu_updated:
        #     R[3,3] = 1.0
        #     R[4,4] = 5.0
        # else:
        #     R[3,3] = 10.0
        #     R[4,4] = 50.0
        # if se.vel_updated:
        #     R[2, 2] = 1.0
        #     R[10, 10] = 1.0
        # else:
        #     R[2, 2] = 1.0
        #     R[10, 10] = 1.0
        se.x_meas = polyval(se.c_X, t_now)
        se.y_meas = polyval(se.c_Y, t_now)
        se.gps_updated = False
        se.imu_updated = False
        se.vel_updated = False

        # define input
        d_f_hist.append(se.cmd_servo)           # this is for a 0.2 seconds delay of steering
        d_f_lp = d_f_lp + 0.5*(se.cmd_servo-d_f_lp) # low pass filter on steering
        a_lp = a_lp + 1.0*(se.cmd_motor-a_lp)       # low pass filter on acceleration
        #u = [a_lp, d_f_hist.pop(0)]
        u = [se.cmd_motor, d_f_hist.pop(0)]

        bta = 0.5 * u[1]
        # get measurement
        y = array([se.x_meas, se.y_meas, se.vel_meas, se.yaw_meas, se.psiDot_meas, se.a_x_meas, se.a_y_meas,
                    se.x_meas, se.y_meas, se.yaw_meas, se.vel_meas, cos(bta)*se.vel_meas, sin(bta)*se.vel_meas])

        # build extra arguments for non-linear function
        args = (u, vhMdl, dt, 0)

        # apply EKF and get each state estimate
        (z_EKF, P) = ekf(f_SensorKinematicModel, z_EKF, P, h_SensorKinematicModel, y, Q, R, args)
        # Read values
        (x_est, y_est, v_x_est, v_y_est, a_x_est, a_y_est, psi_est, psi_dot_est, psi_drift_est,
            x_est_2, y_est_2, psi_est_2, v_est_2, psi_drift_est_2) = z_EKF           # note, r = EKF estimate yaw rate

        se.x_est = x_est_2
        se.y_est = y_est_2

        # Update track position
        l.set_pos(x_est_2, y_est_2, psi_est_2, v_x_est, v_y_est, psi_dot_est)

        # Calculate new s, ey, epsi (only 12.5 Hz, enough for controller that runs at 10 Hz)
        if est_counter%4 == 0:
            l.find_s()
        #l.s = 0
        #l.epsi = 0
        #l.s_start = 0

        # and then publish position info
        ros_t = rospy.get_rostime()
        state_pub_pos.publish(pos_info(Header(stamp=ros_t), l.s, l.ey, l.epsi, v_est_2, l.s_start, l.x, l.y, l.v_x, l.v_y,
                                       l.psi, l.psiDot, se.x_meas, se.y_meas, se.yaw_meas, se.vel_meas, se.psiDot_meas,
                                       psi_drift_est, a_x_est, a_y_est, se.a_x_meas, se.a_y_meas, se.cmd_motor, se.cmd_servo,
                                       (0,), (0,), (0,), l.coeffCurvature.tolist()))

        # wait
        est_counter += 1
        rate.sleep()

if __name__ == '__main__':
    try:
        state_estimation()
    except rospy.ROSInterruptException:
        pass
