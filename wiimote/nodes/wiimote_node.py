#!/usr/bin/python
################################################################################
#
# File:         wiimode_node.py
# RCS:          $Header: $
# Description:  Top level ROS node that publishes Wiimote data
#               and allows Wiimote rumble/LED setting.
# Author:       Andreas Paepcke
# Created:      Thu Sep 10 10:31:44 2009
# Modified:     Thu Sep 24 14:41:10 2009 (Andreas Paepcke) paepcke@anw.willowgarage.com
# Language:     Python
# Package:      N/A
# Status:       Experimental (Do Not Distribute)
#
# (c) Copyright 2009, Willow Garage, all rights reserved.
#
################################################################################

#!/usr/bin/env python

"""The wiimote_node broadcasts three topics, and listens to messages that control
the Wiimote stick's rumble (vibration) and LEDs. Transmitted topics (@100Hz):

   o /joy        Messages contain the accelerometer and gyro axis data, and all button states.
   o /imu_data   Messages contain the accelerometer and gyro axis data, and covariance matrices
   o /wiimote    Messages contain the accelerometer and gyro axis data (both zeroed as in
                 the /joy and /imu_data messages, and raw), the button states, the LED states,
                 rumble (i.e. vibrator) state, IR light sensor readings, time since last zeroed, 
                 and battery state. See Wiimote.msg
                 
The node listens to the following messages:

   o /rumble     Instruct this node to turn on/off the rumble (i.e. vibrator). Rather
                 than just switching rumble, the message can instead contain
                 an array of on/off durations. This node will pulse the rumbler
                 accordingly without the message sender's additional cooperation.
                 See RumbleControl.mg and TimedSwitch.msg
   o /led        Turn each LED on the Wiimote on/off. The message can instead 
                 contain an array of TimedSwitch structures. Each such structure
                 turns a respective LED on and off according to time intervals that
                 are stored in the structure. See LEDControl.msg and TimedSwitch.msg
                 
No command line parameters.
"""

# Code structure: The main thread spawns one thread each for the 
# three message senders, and one thread each for the message listeners.
# The respective classes are IMUSender, JoySender, and WiiSender for
# topic sending, and WiimoteListeners for the two message listeners.
#
# The Wiimote driver is encapsulated in class WIIMote (see WIIMote.py).
# That class' singleton instance runs in its own thread, and uses 
# the third-party cwiid access software.


# TODO: Removal of gyro is noticed (covar[0,0]<--1). But software does not notice plugging in.
# TODO: Command line option: --no-zeroing

# -------- Python Standard Modules:
import sys
import threading
import traceback

# -------- ROS-Related Modules:
import roslib; roslib.load_manifest('wiimote')
import rospy
from geometry_msgs.msg import Vector3
from geometry_msgs.msg import Point
from sensor_msgs.msg import Imu
from joy.msg import Joy
from wiimote.msg import Wiimote
from wiimote.msg import TimedSwitch
from wiimote.msg import LEDControl
from wiimote.msg import RumbleControl

# -------- WIIMote Modules:
from wiimote.wiimoteExceptions import *
from wiimote.wiimoteConstants import *
import wiimote.WIIMote

def runWiimoteNode():
    """Initialize the wiimote_node, establishing its name for communication with the Master"""

    # All exceptions will end up in the __main__ section
    # and are handled there:
    
    rospy.init_node('wiimote', anonymous=True, log_level=rospy.ERROR) # log_level=rospy.DEBUG
    wiimoteDevice = wiimote.WIIMote.WIIMote()
    wiimoteDevice.zeroDevice()
    try:
        IMUSender(wiimoteDevice, freq=100).start()
        JoySender(wiimoteDevice, freq=100).start()
        WiiSender(wiimoteDevice, freq=100).start()
        WiimoteListeners(wiimoteDevice).start()
        
        rospy.spin()
        
    except:    
        raise
    finally:
        wiimoteDevice.setRumble(False)
        wiimoteDevice.setLEDs([False, False, False, False])
        wiimoteDevice.shutdown()


class WiimoteDataSender(threading.Thread):
    
    def __init__(self, wiiMote, freq=100):
        
        threading.Thread.__init__(self)
        self.wiiMote = wiiMote
        self.freq = freq
        self.sleepDuration = 1.0 / freq
        
        varianceAccelerator = self.wiiMote.getVarianceAccelerator();
        self.linear_acceleration_covariance = [varianceAccelerator[X], 0., 0.,
                                               0., varianceAccelerator[Y], 0.,
                                               0., 0., varianceAccelerator[Z]]

        varianceGyro = self.wiiMote.getVarianceGyro();
        self.angular_velocity_covariance = [varianceGyro[X], 0., 0.,
                                            0., varianceGyro[Y], 0.,
                                            0., 0., varianceGyro[Z]]
        
        # If no gyro is attached to the Wiimote then we signal
        # the invalidity of angular rate w/ a covariance matrix
        # whose first element is -1:
        self.gyroAbsence_covariance = [-1., 0., 0.,
                                       0., 0., 0.,
                                       0., 0., 0.]
    
    def obtainWiimoteData(self):
        """Retrieve one set of Wiimote measurements from the Wiimote instance. Return scaled accelerator and gyro readings.
        
        We canonicalize both accelerator and gyro data through
        scaling them by constants that turn them into m/sec^2, and 
        radians/sec, respectively.
        
        Return: list of canonicalized accelerator and gyro readings. 
        """
        
        while not rospy.is_shutdown():
            self.wiistate = self.wiiMote.getWiimoteState()
            if self.wiistate is not None and self.wiistate.acc is not None:
                break
            else:
                rospy.sleep(0.1)
            
        return self.canonicalizeWiistate()
        
    def canonicalizeWiistate(self):
        """Scale accelerator and gyro readings to be m/sec^2, and radians/sec, respectively."""
        
        # Convert acceleration, which is in g's into m/sec^@:
        canonicalAccel = self.wiistate.acc.scale(EARTH_GRAVITY)
            
        # If the gyro is connected, then 
        # Convert gyro reading to radians/sec (see wiimoteConstants.py
        # for origin of this scale factor):
        if self.wiistate.motionPlusPresent:
            canonicalAngleRate = self.wiistate.angleRate.scale(GYRO_SCALE_FACTOR)
        else:
             canonicalAngleRate = None
        
        return [canonicalAccel, canonicalAngleRate]


            
class IMUSender(WiimoteDataSender):
    """Broadcasting Wiimote accelerator and gyro readings as IMU messages to Topic imu_data"""
    
    def __init__(self, wiiMote, freq=100):
        """Initializes the Wiimote IMU publisher.
    
        Parameters:
            wiiMote: a bluetooth-connected, calibrated WIIMote instance
            freq:    the message sending frequency in messages/sec. Max is 100, because
                     the Wiimote only samples the sensors at 100Hz.
        """
        
        WiimoteDataSender.__init__(self, wiiMote, freq)
        
        self.pub = rospy.Publisher('imu_data', Imu)        
        
    def run(self):
        """Loop that obtains the latest wiimote state, publishes the IMU data, and sleeps.
        
        The IMU message, if fully filled in, contains information on orientation,
        acceleration (in m/s^2), and angular rate (in radians/sec). For each of
        these quantities, the IMU message format also wants the corresponding
        covariance matrix.
        
        Wiimote only gives us acceleration and angular rate. So we ensure that the orientation
        data entry is marked invalid. We do this by setting the first
        entry of its associated covariance matrix to -1. The covariance
        matrices are the 3x3 matrix with the axes' variance in the 
        diagonal. We obtain the variance from the Wiimote instance.  
        """
        
        rospy.loginfo("Wiimote IMU publisher starting (topic /imu_data).")
        try:
            while not rospy.is_shutdown():
                (canonicalAccel, canonicalAngleRate) = self.obtainWiimoteData()
                
                msg = Imu(header=None,
                          orientation=None,                                         # will default to [0.,0.,0.,0],
                          orientation_covariance=[-1.,0.,0.,0.,0.,0.,0.,0.,0.],     # -1 indicates that orientation is unknown
                          angular_velocity=None,
                          angular_velocity_covariance=self.angular_velocity_covariance,
                          linear_acceleration=None,
                          linear_acceleration_covariance=self.linear_acceleration_covariance)
                          
                    
                # If a gyro is plugged into the Wiimote, then note the 
                # angular velocity in the message, else indicate with
                # the special gyroAbsence_covariance matrix that angular
                # velocity is unavailable:      
                if self.wiistate.motionPlusPresent:
                    msg.angular_velocity.x = canonicalAngleRate[PHI]
                    msg.angular_velocity.y = canonicalAngleRate[THETA]
                    msg.angular_velocity.z = canonicalAngleRate[PSI]
                else:
                    msg.angular_velocity_covariance = self.gyroAbsence_covariance
                
                msg.linear_acceleration.x = canonicalAccel[X]
                msg.linear_acceleration.y = canonicalAccel[Y]
                msg.linear_acceleration.z = canonicalAccel[Z]
                
                measureTime = self.wiistate.time
                timeSecs = int(measureTime)
                timeNSecs = int(abs(timeSecs - measureTime) * 10**9)
                msg.header.stamp.secs = timeSecs
                msg.header.stamp.nsecs = timeNSecs
                
                self.pub.publish(msg)
                
                rospy.logdebug("IMU state:")
                rospy.logdebug("    IMU accel: " + str(canonicalAccel) + "\n    IMU angular rate: " + str(canonicalAngleRate))
                rospy.sleep(self.sleepDuration)
        except rospy.ROSInterruptException:
            rospy.loginfo("Shutdown request. Shutting down Imu sender.")
            exit(0)
            
            
class JoySender(WiimoteDataSender):
    
    """Broadcasting Wiimote accelerator and gyro readings as Joy(stick) messages to Topic joy"""
    
    def __init__(self, wiiMote, freq=100):
        """Initializes the Wiimote Joy(stick) publisher.
    
        Parameters:
            wiiMote: a bluetooth-connected, calibrated WIIMote instance
            freq:    the message sending frequency in messages/sec. Max is 100, because
                     the Wiimote only samples the sensors at 100Hz.
        """
        
        WiimoteDataSender.__init__(self, wiiMote, freq)

        
        self.pub = rospy.Publisher('joy', Joy)        
        
    def run(self):
        """Loop that obtains the latest wiimote state, publishes the joystick data, and sleeps.
        
        The Joy.msg message types calls for just two fields: float32[] axes, and int32[] buttons.
        """
        
        rospy.loginfo("Wiimote joystick publisher starting (topic /joy).")
        try:
            while not rospy.is_shutdown():
                (canonicalAccel, canonicalAngleRate) = self.obtainWiimoteData()
                
                msg = Joy(# the Joy msg does not have a header :-( header=None,
                          axes=[canonicalAccel[X], canonicalAccel[Y], canonicalAccel[Z]],
                          buttons=None)
                
                # If a gyro is attached to the Wiimote, we add the
                # gyro information:
                if self.wiistate.motionPlusPresent:
                    msg.axes.extend([canonicalAngleRate[PHI], canonicalAngleRate[THETA], canonicalAngleRate[PSI]])
                          
                theButtons = []
                theButtons.append(self.wiistate.buttons[BTN_1])
                theButtons.append(self.wiistate.buttons[BTN_2])
                theButtons.append(self.wiistate.buttons[BTN_PLUS])
                theButtons.append(self.wiistate.buttons[BTN_MINUS])
                theButtons.append(self.wiistate.buttons[BTN_A])
                theButtons.append(self.wiistate.buttons[BTN_B])
                theButtons.append(self.wiistate.buttons[BTN_UP])
                theButtons.append(self.wiistate.buttons[BTN_DOWN])
                theButtons.append(self.wiistate.buttons[BTN_LEFT])
                theButtons.append(self.wiistate.buttons[BTN_RIGHT])
                theButtons.append(self.wiistate.buttons[BTN_HOME])
                
                msg.buttons = theButtons
                
                measureTime = self.wiistate.time
                timeSecs = int(measureTime)
                timeNSecs = int(abs(timeSecs - measureTime) * 10**9)
                # the Joy msg does not have a header :-(
                # msg.header.stamp.secs = timeSecs
                # msg.header.stamp.nsecs = timeNSecs
                
                self.pub.publish(msg)
                
                rospy.logdebug("Joystick state:")
                rospy.logdebug("    Joy buttons: " + str(theButtons) + "\n    Joy accel: " + str(canonicalAccel) + "\n    Joy angular rate: " + str(canonicalAngleRate))
                rospy.sleep(self.sleepDuration)
        except rospy.ROSInterruptException:
            rospy.loginfo("Shutdown request. Shutting down Joy sender.")
            exit(0)

class WiiSender(WiimoteDataSender):
    """Broadcasting complete Wiimote messages to Topic wiimote"""
    
    def __init__(self, wiiMote, freq=100):
        """Initializes the full-Wiimote publisher.
    
        Parameters:
            wiiMote: a bluetooth-connected, calibrated WIIMote instance
            freq:    the message sending frequency in messages/sec. Max is 100, because
                     the Wiimote only samples the sensors at 100Hz.
        """
        
        WiimoteDataSender.__init__(self, wiiMote, freq)
        
        self.pub = rospy.Publisher('wiimote', Wiimote)        
        
    def run(self):
        """Loop that obtains the latest wiimote state, publishes the data, and sleeps.
        
        The wiimote message, if fully filled in, contains information in common with imu_data.msg:
        acceleration (in m/s^2), and angular rate (in radians/sec). For each of
        these quantities, the IMU message format also wants the corresponding
        covariance matrix.
        
        The covariance matrices are the 3x3 matrix with the axes' variance in the 
        diagonal. We obtain the variance from the Wiimote instance.  
        """
        
        rospy.loginfo("Wiimote publisher starting (topic /wiimote).")
        try:
            while not rospy.is_shutdown():
                (canonicalAccel, canonicalAngleRate) = self.obtainWiimoteData()
                
                msg = Wiimote(header=None,
                          angular_velocity_zeroed=None,
                          angular_velocity_raw=None,
                          angular_velocity_covariance=self.angular_velocity_covariance,
                          linear_acceleration_zeroed=None,
                          linear_acceleration_raw=None,
                          linear_acceleration_covariance=self.linear_acceleration_covariance,
                          buttons=[0,0,0,0,0,0,0,0,0,0],
                          rumble=0,
                          LEDs=None,
                          ir_positions=None,
                          battery=None,
                          zeroing_time=self.wiiMote.lastZeroingTime,
                          errors=0)
                    
                # If a gyro is plugged into the Wiimote, then note the 
                # angular velocity in the message, else indicate with
                # the special gyroAbsence_covariance matrix that angular
                # velocity is unavailable:      
                if self.wiistate.motionPlusPresent:
                    msg.angular_velocity_zeroed.x = canonicalAngleRate[PHI]
                    msg.angular_velocity_zeroed.y = canonicalAngleRate[THETA]
                    msg.angular_velocity_zeroed.z = canonicalAngleRate[PSI]
                    
                    msg.angular_velocity_raw.x = self.wiistate.angleRateRaw[PHI]
                    msg.angular_velocity_raw.y = self.wiistate.angleRateRaw[THETA]
                    msg.angular_velocity_raw.z = self.wiistate.angleRateRaw[PSI]
                    
                else:
                    msg.angular_velocity_covariance = self.gyroAbsence_covariance
                
                msg.linear_acceleration_zeroed.x = canonicalAccel[X]
                msg.linear_acceleration_zeroed.y = canonicalAccel[Y]
                msg.linear_acceleration_zeroed.z = canonicalAccel[Z]
                
                msg.linear_acceleration_raw.x = self.wiistate.accRaw[X]
                msg.linear_acceleration_raw.y = self.wiistate.accRaw[Y]
                msg.linear_acceleration_raw.z = self.wiistate.accRaw[Z]
                
                theButtons = []
                theButtons.append(self.wiistate.buttons[BTN_1])
                theButtons.append(self.wiistate.buttons[BTN_2])
                theButtons.append(self.wiistate.buttons[BTN_PLUS])
                theButtons.append(self.wiistate.buttons[BTN_MINUS])
                theButtons.append(self.wiistate.buttons[BTN_A])
                theButtons.append(self.wiistate.buttons[BTN_B])
                theButtons.append(self.wiistate.buttons[BTN_UP])
                theButtons.append(self.wiistate.buttons[BTN_DOWN])
                theButtons.append(self.wiistate.buttons[BTN_LEFT])
                theButtons.append(self.wiistate.buttons[BTN_RIGHT])
                theButtons.append(self.wiistate.buttons[BTN_HOME])
                
                ledStates = self.wiiMote.getLEDs()
                for indx in range(len(msg.LEDs)):
                    if ledStates[indx]: 
                        msg.LEDs[indx] = 1
                    else:
                        msg.LEDs[indx] = 0
                
                msg.buttons = theButtons
    
                batteryRaw = self.wiiMote.getBattery()
                msg.battery[BATTERY_RAW] = batteryRaw
                msg.battery[BATTERY_PERCENTAGE] = batteryRaw * 100./self.wiiMote.BATTERY_MAX
    
                irSources = self.wiistate.IRSources
                
                for irSensorIndx in range(NUM_IR_SENSORS):
                    if irSources[irSensorIndx] is not None:
                        # Did hardware deliver IR source position for this IR sensor?
                        try:
                          pos  = irSources[irSensorIndx]['pos']
                        except KeyError:
                            # If no position information from this IR sensor, use -1 for the dimensions:
                            msg.ir_positions[irSensorIndx] = Point(-1, -1, -1)
                        else:
                            # Have IR position info from this IR sensor. We use the 3D Point
                            # message and set the z-dimension to -1:
                            msg.ir_positions[irSensorIndx] = Point(pos[0], pos[1], -1)
                            
                            # Same exercise for the IR source size measurement that the Wii driver might provide:
                            try: 
                                size = irSources[irSensorIndx]['size']
                            except KeyError:
                                # If the driver did not deliver size information, indicate by using -1:
                                msg.ir_sizes[irSensorIndx] = -1
                            else:
                                msg.ir_sizes[irSensorIndx] = size
                    else:
                        msg.ir_positions[irSensorIndx] = Point(-1, -1, -1)
                        msg.ir_sizes[irSensorIndx] = -1
                
                measureTime = self.wiistate.time
                timeSecs = int(measureTime)
                timeNSecs = int(abs(timeSecs - measureTime) * 10**9)
                msg.header.stamp.secs = timeSecs
                msg.header.stamp.nsecs = timeNSecs
                
                self.pub.publish(msg)
                
                rospy.logdebug("Wiimote state:")
                rospy.logdebug("    Accel: " + str(canonicalAccel) + "\n    Angular rate: " + str(canonicalAngleRate))
                rospy.logdebug("    Rumble: " + str(msg.rumble) + "\n    Battery: [" + str(msg.battery[0]) + "," + str(msg.battery[1]))
                rospy.logdebug("    IR positions: " + str(msg.ir_positions) + " IR sizes: " + str(msg.ir_sizes))
                                
                rospy.sleep(self.sleepDuration)
        except rospy.ROSInterruptException:
            rospy.loginfo("Shutdown request. Shutting down Wiimote sender.")
            exit(0)
        
class WiimoteListeners(threading.Thread):
    """Listen for request to rumble and LED blinking.
    
    Subscribes to topics /rumble and /leds, listening for RumbleControl 
    and LEDControl messages.
    
    Parameters: The switch_mode field is either
    -1.0 to turn rumble on, zero to turn it off, or a 
    positive float. If switch_mode is such a positive number,
    it is taken to be the repeat count for an on/off rumble
    pattern (see next parameter) 
    
    The pulse_pattern is a float32[MAX_RUMBLE_PATTERN_LENGTH],
    which contains fractional seconds that rumble is to be
    on or off.
    """    
    
    def __init__(self, wiiMote):
        
        threading.Thread.__init__(self)
        self.wiiMote = wiiMote    
        self.pulserThread = None  
        
    def run(self):
        
      def rumbleSwitchCallback(msg):
        """Callback for turning rumble on/off, and to initiate pulse rumble."""
        
        rospy.logdebug("From: " + rospy.get_caller_id() + ". Rumble request \n" + str(msg))
        
        # If a rumble pulser thread is running, stop it:
        if self.pulserThread is not None:
            self.pulserThread.stop = True 
            # Wait for the thread to finish what it's doing
            self.pulserThread.join()
            self.pulserThread = None
             
        if msg.rumble.switch_mode == SWITCH_ON:
            self.wiiMote.setRumble(True)
            return
        elif msg.rumble.switch_mode == SWITCH_OFF:
            self.wiiMote.setRumble(False)
            return
        elif msg.rumble.switch_mode != SWITCH_PULSE_PATTERN:
            rospy.loginfo("Illegal switch_mode value in rumble request from " + \
                     rospy.get_caller_id() + \
                     ": \n" + \
                     str(msg))
            return
            
        # Client wants to start a rumble pulse sequence:
        if msg.rumble.num_cycles == 0:
            return
            
        # Pulser takes an *array* of OutputPattern. For rumble that array is
        # always of length 1. But for other feedback indicators, like LEDs,
        # there are more:
        self.pulserThread = SwitchPulser([OutputPattern(msg.rumble.pulse_pattern, msg.rumble.num_cycles)], RUMBLE, self.wiiMote)
        self.pulserThread.start()
        self.pulserThread.join()
        
        return # end rumbleSwitchCallback
        
        
      def ledControlCallback(msg):
        """Callback for incoming LEDCOntrol requests."""
        
        rospy.logdebug(rospy.get_caller_id() + "LED Control request " + str(msg))
        
        # Are they using the simple switch_array field, or the
        # more complex TimedSwitch array?
        
        if msg.switch_array[0] != -1:
            # Simply set the LEDs appropriately, and be done:
            self.wiiMote.setLEDs(msg.switch_array)
            return
        
        # Each LED has a TimedSwitch associated with it. Unpack
        # the data structure (an array of TimedSwitch) for passage
        # to the SwitchPulser thread. We need to pull out the 
        # number of requested cycles for each LED's on/off pattern,
        # and the pattern arrays themselves:
        
        patterns = []
        
        for timedSwitch in msg.timed_switch_array:
            patterns.append(OutputPattern(timedSwitch.pulse_pattern, timedSwitch.num_cycles))
            
        self.pulserThread = SwitchPulser(patterns, LED, self.wiiMote)
        self.pulserThread.start()
        self.pulserThread.join()
        
        return  # end ledControlCallback()

      # Done with embedded function definitions. Back at the top
      # level of WiimoteListeners' run() function.
       
      # Subscribe to rumble and LED control messages and sit:
      rospy.loginfo("Wiimote rumble listener starting (topic /rumble).")
      rospy.Subscriber("rumble", RumbleControl, rumbleSwitchCallback)
      rospy.loginfo("Wiimote LED control listener starting (topic /leds).")
      rospy.Subscriber("leds", LEDControl, ledControlCallback)
      
      try:
          rospy.spin()
      except rospy.ROSInterruptException:
        rospy.loginfo("Shutdown request. Shutting down Wiimote listeners.")
        exit(0)
          
class SwitchPulser(threading.Thread):
    """Thread for executing rumble and LED pulse patterns."""
    
    def __init__(self, patternArray, outputIndicator, wiimoteDevice):
        """Parameters: 
        o patternArray: For each pattern: an OutputPattern object
          There will only be one such object for Rumble output.
          For LEDs there will  be one for each LED on the Wiimote.
          If one of the elements is None, that output indicator
          is left unchanged. For example, if the 2nd element
          in an LED pattern object array is None, the 2nd LED on the
          Wiimote will be left in its current state.
          
          Note that the patterns may be of different lengths.
          So, one LED might have a 3-state pattern, while another
          LED's pattern is 5 states long.
        o RUMBLE or LED to indicate what is to be pulsed
        o A Wiimote device object
        
        Note: We always start the affected indicators as if they were
              in the OFF state, and we always leave them in the off state.
        """
        
        threading.Thread.__init__(self)
        self.patternArray = patternArray
        self.wiimoteDevice = wiimoteDevice
        # Whether to pulse rumble or LEDs
        self.outputIndicator = outputIndicator
        # Allow this thread to be stopped by setting
        # instance variable 'stop' to True:
        self.stop = False
        
    def run(self):

        rospy.logdebug("In pulser thread. patternArray: " + str(self.patternArray) + ". Len: " + str(len(self.patternArray)))
            
        # First state is always ON:
        self.turnIndicatorOn(self.outputIndicator)
            
        numPatterns = len(self.patternArray)
        try:
            while not rospy.is_shutdown() and not self.stop:

                # Initialize nextDuration for sleeping to infinity:
                nextDuration = float('inf')
        
                # Get the next sleep duration, which is the
                # minimum of all nextDurations times:
                
                for pattern in self.patternArray:
                    
                    if pattern is None: continue
                    patternHeadTime = pattern.timeRemaining()
                    if patternHeadTime is None: continue
                    nextDuration = min(nextDuration, patternHeadTime)
                    
                # All patterns done?
                if nextDuration == float('inf'):
                    rospy.logdebug("End of pattern.")
                    exit(0)
                
                rospy.sleep(nextDuration)
                
                # Time for a state change in at least one of the
                # patterns. We:
                #     o Flip the state of the respective output indicator(s)
                #     o Obtain the next duration entry in the pattern(s) with
                #       the timeout we just finished. 
                #     o We subtract the amout of sleep that we just
                #       awoke from in all other nextDurations
                #     o We find the new minimum next delay and sleep

                durationJustFinished = nextDuration
                for pattern, patternIndex in zip(self.patternArray, range(numPatterns)):
                    
                    # reduceTimer() returns pattern header minus given time duration,
                    # or None if the pattern is spent. As side effect
                    # this operation also takes care of the repeats:
                    
                    reducedTime = pattern.reduceTimer(durationJustFinished)
                    # If this call started the pattern over, we
                    # need to turn the indicator(s) on during this
                    # coming new duration:
                    
                    if pattern.startOfRepeat:
                        self.turnIndicatorOn(self.outputIndicator)
                    
                    if reducedTime is None: continue
                    if reducedTime == 0.:
                        # This pattern had a timeout:
                        self.flipRumbleOrLED(patternIndex)
                        
            # continue while not rospy.is_shutdown() and not self.stop
                
        except rospy.ROSInterruptException:
            rospy.loginfo("Shutdown request. Shutting down pulse switcher.")
        finally:
            # Make sure that the indicators that we manipulated 
            # get turned off:
            
            if self.outputIndicator == RUMBLE:
                self.wiimoteDevice.setRumble(False)
                exit(0)
            elif self.outputIndicator == LED:
                for oneLED, LEDIndex in zip(self.LEDMask, range(len(self.LEDMask))):
                    if oneLED is not None:
                        self.LEDMask[LEDIndex] = False
                self.wiimoteDevice.setLEDs(self.LEDMask)
                
    
    
    def turnIndicatorOn(self, theIndicator):
        """Turns indicator(s) of interest ON.
        
        Parameter: RUMBLE or LED
        """ 
        
        # Recall: patterns are None, we must leave 
        # the respective output Indicator alone:
        
        if theIndicator == RUMBLE and self.patternArray[0] is not None:
            # Start to rumble:
            self.wiimoteDevice.setRumble(True)
            
        # Is this LED action?
        elif theIndicator == LED:
            # Get a clean 4-tuple with True/None depending on
            # whether we have a TimedSwitch for the respective
            # LED. For LEDs for which we don't have 
            # a TimedSwitch: leave those alone:
            
            self.LEDMask = []
            for i in range(min(NUM_LEDS, len(self.patternArray))):
                try:
                    if self.patternArray[i] is None:
                        self.LEDMask.append(None)
                    else:
                        self.LEDMask.append(True)
                except IndexError:
                    pass
                    
            self.wiimoteDevice.setLEDs(self.LEDMask)
        else:
            raise ValueError("Only RUMBLE and LED are legal values here.")
        
    
    def flipRumbleOrLED(self, index=0):

        if self.outputIndicator == RUMBLE:
            self.flipRumble()
        else:
            self.flipLED(index)
             
        
    def flipRumble(self):
        self.wiimoteDevice.setRumble(not self.wiimoteDevice.getRumble())

    
    def flipLED(self, index):
        
        LEDStatus = self.wiimoteDevice.getLEDs()
        
        # None's leave the respective LED unchanged:
        newLEDStatus = [None, None, None, None]
        
        newLEDStatus[index] = not LEDStatus[index] 
        self.wiimoteDevice.setLEDs(newLEDStatus)

class OutputPattern(object):
    """Instances encapsulate rumble or LED on/off time patterns as received from related ROS messages.
    
    This class provides convenient encapsulation for the pattern arrays themselves,
    for associated pointers into the arrays, and for status change and inquiry requests.
    Terminology: 'Pattern Head' is the currently used time duration. A pattern is 'Spent'
    if all the time sequences have run, and no repeats are left.
    
    Public instance variables:
      o startOfRepeat    ; indicates whether pattern just starts to repeat. (see method reduceTimer())
    """

    
    def __init__(self, rosMsgPattern, numReps):
        """Takes a TimedSwitch type ROS message (pattern and number of repeats), and initializes the pointers."""

        # Copy the rosMsgPattern, which is a tuple, and therefore immutable,
        # to an array that we'll be able to modify:
         
        self.origTimePattern = []
        for timeDuration in rosMsgPattern:
            self.origTimePattern.append(timeDuration) 
        
        # Make a working copy of the time series, so that we can subtract from the
        # elements and still have access to the original pattern for repeats:
        
        self.timePattern = self.origTimePattern[:]
        self.numReps = numReps
        # Make first element the pattern head:
        self.patternPt = 0
        self.patternSpent = False
        self.startOfRepeat = False

    def timeRemaining(self):
        """Return the time at the pattern head. If pattern is spent, return None instead."""
        
        if self.patternSpent:
            return None
        return self.timePattern[self.patternPt]
        
    def resetForRepeat(self):
        """Get ready to repeat the pattern. Returns True if another repeat is allowed, else returns False"""
        
        if self.patternSpent:
            return False
        
        self.numReps -= 1
        if self.numReps <= 0:
            return False
        
        # Have at least one repetion of the pattern left: 
        self.patternPt = 0
        
        # Need to start with a fresh copy of the pattern, b/c we 
        # may subtracted time from the elements as we went
        # through the patters in the previous cycle:
        
        self.timePattern = self.origTimePattern[:]
        
        return True
     
    def reduceTimer(self, time):
        """Given a float fractional number of seconds, subtract the time from the pattern head
        
        Returns the remaining time, rounded to 100th of a second, or None. If the
        remaining time after subtraction is <= 0, we check whether any repeats
        are left. If so, we get ready for another repeat, and return the time of the
        first pattern time. Else we return None, indicating that this pattern
        is spent.
        
        After this method returns, this instance's public startOfRepeat variable
        will hold True or False, depending on whether the pattern is
        just starting over.
        """
        
        if self.patternSpent:
            return None
        
        res = self.timePattern[self.patternPt] - time
        # Guard against weird rounding errors:
        if res < 0:
            res = 0.0        
        # Update the head of the pattern:
        self.timePattern[self.patternPt] = res
        res = round(res,2)
        
        self.startOfRepeat = False
        
        if res <= 0:
            
            self.patternPt += 1
            if self.patternPt < len(self.timePattern):
                return res
            
            # Repeat the pattern if there's a rep left:
            canRepeat = self.resetForRepeat()
            
            if canRepeat:
                self.startOfRepeat = True
                # Return as next timeout the first delay
                # of the pattern:
                return self.timePattern[self.patternPt]
            else:
                # Pattern has finished for good, including all repeats:
                self.patternSpent = True
                return None
        return res
    
    def __repr__(self):
        res = "<OutputPattern.  Reps:" + str(self.numReps) + ". Pattern: ["
        for i in range(min(3, len(self.timePattern))):
            res += str(self.timePattern[i]) + ", "
        
        return res + "...]>"
        

if __name__ == '__main__':
    try:
        runWiimoteNode()
    except rospy.ROSInterruptException:
        rospy.rospy.loginfo("ROS Shutdown Request.")
    except KeyboardInterrupt, e:
        rospy.rospy.loginfo("Received keyboard interrupt.")
    except WiimoteNotFoundError, e:
        rospy.logfatal(str(e))
    except WiimoteEnableError, e:
        rospy.logfatal(str(e))
    except CallbackStackMultInstError, e:
        rospy.logfatal(str(e))
    except CallbackStackEmptyError, e:
        rospy.logfatal(str(e))
    except ResumeNonPausedError, e:
        rospy.logfatal(str(e))
    except CallbackStackEmptyError, e:
        rospy.logfatal(str(e))
    
    except:
        excType, excValue, excTraceback = sys.exc_info()[:3]
        traceback.print_exception(excType, excValue, excTraceback)

    finally:
        rospy.rospy.loginfo("Exiting Wiimote node.")
        sys.exit(0)