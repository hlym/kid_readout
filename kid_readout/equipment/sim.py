"""
This module contains classes to interface with SRS SIM hardware.

Commands are case-insensitive but calibration curve identification
strings are converted to uppercase.
"""
from __future__ import division
import os
import time
import serial
import numpy as np
from collections import OrderedDict


class SIMError(Exception):
    pass


class SIM(object):

    termination = '\n'

    boolean_tokens = {'OFF': False,
                      '0': False,
                      'ON': True,
                      '1': True}

    def __init__(self, serial, parent_and_port=(None, None)):
        self.serial = serial
        self.parent = parent_and_port[0]
        self.port = parent_and_port[1]

    def send(self, message):
        if self.parent is None:
            self.serial.write(message + self.termination)
        else:
            self.parent.connect(self.port)
            self.serial.write(message + self.termination)
            self.parent.disconnect()

    def receive(self):
        if self.parent is None:
            return self.serial.readline().strip()
        else:
            self.parent.connect(self.port)
            message = self.serial.readline().strip()
            self.parent.disconnect()
            return message

    def send_and_receive(self, message):
        if self.parent is None:
            self.serial.write(message + self.termination)
            return self.serial.readline().strip()
        else:
            self.parent.connect(self.port)
            self.serial.write(message + self.termination)
            message = self.parent.receive()
            self.parent.disconnect()
            return message

    @property
    def token(self):
        return self.send_and_receive('TOKN?')

    @token.setter
    def token(self, mode):
        if not str(mode).upper() in self.boolean_tokens:
            raise ValueError("Valid token modes are {0}.".format(self.boolean_tokens))
        self.send('TOKN {0}'.format(mode))
    
    @property
    def identity(self):
        return self.send_and_receive('*IDN?')

    def reset(self):
        self.send('*RST')


class SIM900(SIM):

    escape = 'ESCAPE'

    def __init__(self, serial_port, baudrate=9600, timeout=2, autodetect=True):
        self.serial = serial.Serial(port=serial_port, baudrate=baudrate, timeout=timeout)
        self.ports = OrderedDict([('1', None),
                                  ('2', None),
                                  ('3', None),
                                  ('4', None),
                                  ('5', None),
                                  ('6', None),
                                  ('7', None),
                                  ('8', None),
                                  ('9', None),
                                  ('A', None),
                                  ('B', None),
                                  ('C', None),
                                  ('D', None)])
        self.parent = None
        self.disconnect()
        self.reset()
        self.flush()
        self.SIM_reset()
        if autodetect:
            self.autodetect()

    def broadcast(self, message):
        self.send('BRDT "{0}"'.format(message))

    def parse_definite_length(self, message):
        length_bytes = int(message[1])
        return message[2 + length_bytes:]

    def parse_message(self, message):
        """
        Parse a message of the form
        'MSG 1,something'
        and return the port and message:
        ('1', 'something')
        """
        header, content = message.split(',', 1)
        port = header[-1]
        return port, content

    def flush(self, port=None):
        """
        Flush the SIM900 input and output queue for the given port, or for all ports if no port is given.

        This method implements the FLSH command.
        """
        if port is None:
            self.send('FLSH')
        elif str(port) in self.ports:
            self.send('SRST {0}'.format(port))
        else:
            raise ValueError("Invalid port {0}'.format(port)")

    def SIM_reset(self, port=None):
        """
        Send the SIM reset signal to the given SIM port, meaning port
        1 through port 8, or to all SIM ports if no port is specified.
        
        This method implements the SRST command.
        """
        if port is None:
            self.send('SRST')
        elif int(port) in range(1, 9):
            self.send('SRST {0}'.format(int(port)))
        else:
            raise ValueError("Invalid port {0}'.format(port)")

    def connect(self, port):
        if self.connected is not None:
            raise ValueError("Connected to port {0}".format(self.connected))
        self.send('CONN {0}, "{1}"'.format(port, self.escape))
        self.connected = port

    def disconnect(self):
        self.send(self.escape)
        self.connected = None

    def autodetect(self):
        # Upgrade with methods when available.
        self.send('BRER 510') # Turn on broadcasting for ports 1-8
        self.send('RPER 510') # Turn on pass-through for ports 1-8
        self.broadcast('*IDN?') # Ask everything for its identification
        self.send('WAIT 1000') # Wait for one second
        self.send('BRER 0') # Turn off broadcasting
        self.send('RPER 0') # Turn off message pass-through. Check that this keeps self-sent messages on!
        lines = [line.strip() for line in self.serial.readlines()]
        lines = [line for line in lines if line] # Remove blank messages
        for line in lines:
            port, message = self.parse_message(line)
            SRS, sim, serial_number, firmware_version = self.parse_definite_length(message).split(',')
            try:
                self.ports[port] = globals()[sim](self.serial, (self, port)) # Update
            except KeyError as e:
                self.ports[port] = str(e)

class SIMThermometer(SIM):
    """
    This is intended to be an abstract class that allows the
    temperature sensors to share code.

    For the SIM921 resistance bridge, the parameter number refers to
    calibration curve 1, 2, or 3. For the SIM922 diode temperature
    monitor, the parameter number refers to diode channel 1, 2, 3, or
    4; there can be only one user calibration curve stored per channel.
    """

    # This is the maximum fractional error used by the
    # validate_curve() method. It allows for a small rounding error
    # due to limited storage space in the SIM.  I have seen fractional
    # errors of up to 1.2e-5 or so.
    maximum_fractional_error = 1e-4

    def curve_info(self, number):
        message = self.send_and_receive('CINI? {0}'.format(number)).split(',')
        format = message[0]
        identification = message[1]
        points = int(message[2])
        return format, identification, points

    def initialize_curve(self, number, format, identification):
        self.send('CINI {0}, {1}, {2}'.format(number, format, identification))

    def read_curve(self, number):
        format, identification, points = self.curve_info(number)
        sensor = []
        temperature = []
        # The indexing is one-based.
        for n in range(1, points + 1):
            # The SIM921 separator is a comma, as its manual says, but
            # the SIM922 separator is a space and its manual lies.
            message = self.send_and_receive('CAPT? {0}, {1}'.format(number, n)).split(self.CAPT_separator)
            sensor.append(float(message[0]))
            temperature.append(float(message[1]))
        return CalibrationCurve(sensor, temperature, identification, format)

    def write_curve(self, number, curve):
        if curve.sensor.size > self.maximum_temperature_points:
            raise SIMError("Curve contains too many points.")
        self.initialize_curve(number, curve.format, curve.identification)
        if self.parent is not None:
            self.parent.connect(self.port)
        for n in range(curve.sensor.size):
            self.serial.write('CAPT {0}, {1}, {2}{3}'.format(number, curve.sensor[n], curve.temperature[n], self.termination))
            time.sleep(self.write_delay)
        if self.parent is not None:
            self.parent.disconnect()
        if not self.validate_curve(number, curve):
            raise SIMError("Curve data was not written correctly.")

    def validate_curve(self, number, curve):
        format, identification, points = self.curve_info(number)
        stored = self.read_curve(number)
        # If the writing speed is too fast some points may be skipped,
        # which will cause the array comparisons below to raise a
        # ValueError.
        try:
            return (np.all(abs(stored.sensor / curve.sensor - 1) <
                           self.maximum_fractional_error) and
                    np.all(abs(stored.temperature / curve.temperature - 1) <
                           self.maximum_fractional_error) and
                    (stored.identification == curve.identification) and
                    (stored.format == curve.format))
        except ValueError:
            return False


class SIM921(SIMThermometer):

    # The documentation for the CAPT command is correct.
    CAPT_separator = ','

    # The manual doesn't mention the maximum number of points per
    # curve, but with the other system we ran into problems when using
    # more points.
    maximum_temperature_points = 225

    # This is in seconds; points were occasionally dropped at 0.1 seconds.
    write_delay = 0.5

    # Minimum and maximum excitation frequencies in Hz:
    minimum_frequency = 1.95
    maximum_frequency = 61.1

    # Excitation commands
    
    @property
    def frequency(self):
        """
        The excitation frequency in Hz.

        This property implements the FREQ(?) command.
        """
        return float(self.send_and_receive('FREQ?'))

    @frequency.setter
    def frequency(self, frequency):
        if not self.minimum_frequency <= frequency <= self.maximum_frequency:
            raise ValueError("Valid excitation frequency range is from {0} to {1} Hz".format(self.minimum_frequency, self.maximum_frequency))
        self.send('FREQ {0}'.format(frequency))

    @property
    def range(self):
        """
        The resistance range code. See the manual for code meanings.

        This property implements the RANG(?) command.
        """
        return int(self.send_and_receive('RANG?'))

    @range.setter
    def range(self, code):
        if not int(code) in range(10):
            raise ValueError("Valid range codes are integers 0 through 9.")
        self.send('RANG {0}'.format(int(code)))

    @property
    def excitation(self):
        """
        The voltage excitation code. See the manual for code meanings.

        This property implements the EXCI(?) command.
        """
        return int(self.send_and_receive('EXCI?'))
        
    @excitation.setter
    def excitation(self, code):
        if not int(code) in range(-1, 9):
            raise ValueError("Valid excitation codes are integers -1 through 8.")
        self.send('EXCI {0}'.format(int(code)))

    @property
    def excitation_state(self):
        """
        The excitation state.

        This property implements the EXON(?) command.
        """
        return self.send_and_receive('EXON?')

    @excitation_state.setter
    def excitation_state(self, state):
        if not str(state).upper() in self.boolean_tokens:
            raise ValueError("Valid excitation states are {0}.".format(self.boolean_tokens))
        self.send('EXON {0}'.format(state))

    @property
    def excitation_mode(self):
        """
        The excitation mode.

        This property implements the MODE(?) command.
        """
        return self.send_and_receive('MODE?')
    
    @excitation_mode.setter
    def excitation_mode(self, mode):
        if not str(mode).upper() in ('PASSIVE', '0', 'CURRENT', '1', 'VOLTAGE', '2', 'POWER', '3'):
            raise ValueError("Invalid excitation mode.")
        self.send('MODE {0}'.format(mode))

    @property
    def excitation_current(self):
        """
        The actual excitation current amplitude, in amperes.

        This property implements the IEXC? command.
        """
        return float(self.send_and_receive('IEXC?'))

    @property
    def excitation_voltage(self):
        """
        The actual excitation voltage amplitude, in volts.

        This property implements the VEXC? command.
        """
        return float(self.send_and_receive('VEXC?'))
        
    # Measurement commands 

    @property
    def resistance(self):
        """
        Return the measured resistance.
        
        This property implements the RVAL? command.
        
        Multiple measurements and streaming are not yet implemented.
        """
        return float(self.send_and_receive('RVAL?'))

    @property
    def resistance_deviation(self):
        """
        The resistance deviation, in ohms, from the setpoint.

        This property implements the RDEV? command.

        Multiple measurements and streaming are not yet implemented.
        """
        return float(self.send_and_receive('RDEV?'))

    @property
    def temperature(self):
        """
        Return the temperature calculated using the current calibration curve.

        This property implements the TVAL? command.

        Multiple measurements and streaming are not yet implemented.
        """
        return float(self.send_and_receive('TVAL?'))

    @property
    def temperature_deviation(self):
        """
        The temperature deviation, in ohms, from the setpoint.

        This property implements the TDEV? command.

        Multiple measurements and streaming are not yet implemented.
        """
        return float(self.send_and_receive('TDEV?'))

    # The PHAS? command is not yet implemented.

    # The TPER(?) command is not yet implemented.

    # The SOUT command is not yet implemented.

    @property
    def display(self):
        """
        The display state. See the manual for meanings.

        This property implements the DISP(?) command.

        Only the range codes are implemented, not the string values.
        """
        return int(self.send_and_receive('DISP?'))

    @display.setter
    def display(self, code):
        if not int(code) in range(9):
            raise ValueError("Valid display codes are integers 0 through 8.")
        self.send('DISP {0}'.format(int(code)))

    # Post-detection processing commands.

    def filter_reset(self):
        """
        Reset the post-detection filter.

        This method implements the FRST command.
        """
        self.send('FRST')

    @property
    def time_constant(self):
        """
        The filter time constant code. See the manual for meanings.

        This property implements the TCON(?) command.
        """
        return int(self.send_and_receive('TCON?'))

    @time_constant.setter
    def time_constant(self, code):
        if not int(code) in range(-1, 7):
            raise ValueError("Valid time constant codes are integers -1 through 6.")
        self.send('TCON {0}'.format(int(code)))

    @property
    def phase_hold(self):
        """
        The phase hold state.

        This property implements the PHLD command.
        """
        return self.send_and_receive('PHLD?')

    @phase_hold.setter
    def phase_hold(self, mode):
        if not str(mode).upper() in self.boolean_tokens:
            raise ValueError("Valid phase hold modes are {0}.".format(self.boolean_tokens))
        self.send('PHLD {0}'.format(mode))

    # Calibration curve commands

    @property
    def display_temperature(self):
        """
        The temperature display mode.

        This property implements the DTEM(?) command.
        """
        return self.send_and_receive('DTEM?')

    @display_temperature.setter
    def display_temperature(self, mode):
        if not str(mode).upper() in self.boolean_tokens:
            raise ValueError("Valid temperature display modes are {0}.".format(self.boolean_tokens))
        self.send('DTEM {0}'.format(mode))

    @property
    def analog_output_temperature(self):
        """
        The analog output mode.

        This property implements the ATEM(?) command.
        """
        return self.send_and_receive('ATEM?')

    @analog_output_temperature.setter
    def analog_output_temperature(self, mode):
        if not str(mode).upper() in self.boolean_tokens:
            raise ValueError("Valid analog output temperature modes are {0}.".format(self.boolean_tokens))
        self.send('A {0}'.format(mode))

    @property
    def active_curve(self):
        """
        The number of the active calibration curve: 1, 2, or 3.

        This property implements the CURV(?) command.
        """
        return int(self.send_and_receive('CURV?'))

    @active_curve.setter
    def active_curve(self, number):
        if not str(number) in ('1', '2', '3'):
            raise ValueError("Curve number must be 1, 2, or 3.")
        self.send('CURV {0}'.format(number))

    # The CINI(?) and CAPT(?) commands are implemented in SIMThermometer.
    
    # Autoranging commands
    
    @property
    def autorange_gain(self):
        """
        The gain autorange mode.

        This property implements the AGAI(?) command.
        """
        return self.send_and_receive('AGAI?')
    
    @autorange_gain.setter
    def autorange_gain(self, mode):
        if not str(mode).upper() in self.boolean_tokens:
            raise ValueError("Valid gain autorange modes are {0}.".format(self.boolean_tokens))
        self.send('AGAI {0}'.format(mode))

    @property
    def autorange_display(self):
        """
        The display autorange mode.

        This property implements the ADIS(?) command.
        """
        return self.send_and_receive('ADIS?')
    
    @autorange_display.setter
    def autorange_display(self, mode):
        if not str(mode).upper() in self.boolean_tokens:
            raise ValueError("Valid dispay autorange modes are {0}.".format(self.boolean_tokens))
        self.send('ADIS {0}'.format(mode))

    def autocalibrate(self):
        """
        Initiate the internal autocalibration cycle.
        """
        self.send('ACAL')

    # Setpoint and analog output commands.
    # Not yet implemented.

    # Interface commands.
    # The *IDN, *RST, and TOKN commands are implemented in SIM.

    # Status commands.
    # Not yet implemented.


        

class SIM922(SIMThermometer):

    # The documentation for the CAPT command is incorrect: the
    # separator is a space, not a comma.
    CAPT_separator = ' '

    # The manual says that this is the maximum number of points per
    # channel, but I haven't checked it yet.
    maximum_temperature_points = 256

    # This is in seconds; points were sometimes dropped at 0.5 seconds and below.
    write_delay = 1

    def voltage(self, channel):
        return float(self.send_and_receive('VOLT? {0}'.format(channel)))

    def temperature(self, channel):
        return float(self.send_and_receive('TVAL? {0}'.format(channel)))

    def get_curve_type(self, channel):
        return self.send_and_receive('CURV? {0}'.format(channel))

    def set_curve_type(self, channel, curve_type):
        if not str(curve_type).upper() in ('0', 'STAN', '1', 'USER'):
            raise ValueError("Invalid curve type.")
        self.send('CURV {0}, {1}'.format(channel, curve_type))


class SIM925(SIM):

        pass


class CalibrationCurve(object):
    
    def __init__(self, sensor, temperature, identification, format='0'):
        """
        This class represents and calibration curve.

        This class stores identification strings as uppercase because
        the hardware stores them that way. This allows the
        validate_curve() method to work.
        """
        self.sensor = np.array(sensor)
        if not all(np.diff(self.sensor)):
            raise ValueError("Sensor values must increase monotonically.")
        self.temperature = np.array(temperature)
        if not self.sensor.size == self.temperature.size:
            raise ValueError("Different numbers of sensor and temperature points.")
        self.identification = str(identification).upper()
        self.format = str(format)


def load_curve(filename, format='0'):
    identification = os.path.splitext(os.path.basename(filename))[0]
    sensor, temperature = np.loadtxt(filename, unpack=True)
    return CalibrationCurve(sensor, temperature, identification, format)


def save_curve(directory, curve, format=('%.5f\t%.5f'), newline='\r\n', extension='.txt'):
    filename = os.path.join(directory, curve.identification + extension)
    columns = np.empty((curve.sensor.size, 2))
    columns[:, 0] = curve.sensor
    columns[:, 1] = curve.temperature
    np.savetxt(filename, columns, fmt=format, newline=newline)
    return filename