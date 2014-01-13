# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: t -*-
# vi: set ft=python sts=4 ts=4 sw=4 noet :

# This file is part of Fail2Ban.
#
# Fail2Ban is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Fail2Ban is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Fail2Ban; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

# Author: Cyril Jaquier
# 

__author__ = "Cyril Jaquier"
__copyright__ = "Copyright (c) 2004 Cyril Jaquier"
__license__ = "GPL"

import time, logging
import os
import imp
from collections import Mapping

from .banmanager import BanManager
from .jailthread import JailThread
from .action import ActionBase, CommandAction, CallingMap
from .mytime import MyTime

# Gets the instance of the logger.
logSys = logging.getLogger(__name__)

class Actions(JailThread, Mapping):
	"""Handles jail actions.

	This class handles the actions of the jail. Creation, deletion or to
	actions must be done through this class. This class is based on the
	Mapping type, and the `add` method must be used to add new actions.
	This class also starts and stops the actions, and fetches bans from
	the jail executing these bans via the actions.
	"""

	def __init__(self, jail):
		"""Initialise an empty Actions instance.

		Parameters
		----------
		jail: Jail
			The jail of which the actions belongs to.
		"""
		JailThread.__init__(self)
		## The jail which contains this action.
		self._jail = jail
		self._actions = dict()
		## The ban manager.
		self.__banManager = BanManager()

	def add(self, name, pythonModule=None, initOpts=None):
		"""Adds a new action.

		Add a new action if not already present, defaulting to standard
		`CommandAction`, or specified Python module.

		Parameters
		----------
		name : str
			The name of the action.
		pythonModule : str, optional
			Path to Python file which must contain `Action` class.
			Default None, which means `CommandAction` is used.
		initOpts : dict, optional
			Options for Python Action, used as keyword arguments for
			initialisation. Default None.

		Raises
		------
		ValueError
			If action name already exists.
		RuntimeError
			If external Python module does not have `Action` class
			or does not implement necessary methods as per `ActionBase`
			abstract class.
		"""
		# Check is action name already exists
		if name in self._actions:
			raise ValueError("Action %s already exists" % name)
		if pythonModule is None:
			action = CommandAction(self._jail, name)
		else:
			pythonModuleName = os.path.basename(pythonModule.strip(".py"))
			customActionModule = imp.load_source(
				pythonModuleName, pythonModule)
			if not hasattr(customActionModule, "Action"):
				raise RuntimeError(
					"%s module does not have 'Action' class" % pythonModule)
			elif not issubclass(customActionModule.Action, ActionBase):
				raise RuntimeError(
					"%s module %s does not implement required methods" % (
						pythonModule, customActionModule.Action.__name__))
			action = customActionModule.Action(self._jail, name, **initOpts)
		self._actions[name] = action

	def __getitem__(self, name):
		try:
			return self._actions[name]
		except KeyError:
			raise KeyError("Invalid Action name: %s" % name)

	def __delitem__(self, name):
		try:
			del self._actions[name]
		except KeyError:
			raise KeyError("Invalid Action name: %s" % name)

	def __iter__(self):
		return iter(self._actions)

	def __len__(self):
		return len(self._actions)

	def __eq__(self, other): # Required for Threading
		return False

	def __hash__(self): # Required for Threading
		return id(self)

	##
	# Set the ban time.
	#
	# @param value the time
	
	def setBanTime(self, value):
		self.__banManager.setBanTime(value)
		logSys.info("Set banTime = %s" % value)
	
	##
	# Get the ban time.
	#
	# @return the time
	
	def getBanTime(self):
		return self.__banManager.getBanTime()

	def removeBannedIP(self, ip):
		"""Removes banned IP calling actions' unban method

		Remove a banned IP now, rather than waiting for it to expire,
		even if set to never expire.

		Parameters
		----------
		ip : str
			The IP address to unban

		Raises
		------
		ValueError
			If `ip` is not banned
		"""
		# Find the ticket with the IP.
		ticket = self.__banManager.getTicketByIP(ip)
		if ticket is not None:
			# Unban the IP.
			self.__unBan(ticket)
		else:
			raise ValueError("IP %s is not banned" % ip)

	def run(self):
		"""Main loop for Threading.

		This function is the main loop of the thread. It checks the jail
		queue and executes commands when an IP address is banned.

		Returns
		-------
		bool
			True when the thread exits nicely.
		"""
		self.setActive(True)
		for name, action in self._actions.iteritems():
			try:
				action.start()
			except Exception as e:
				logSys.error("Failed to start jail '%s' action '%s': %s",
					self._jail.getName(), name, e)
		while self._isActive():
			if not self.getIdle():
				#logSys.debug(self._jail.getName() + ": action")
				ret = self.__checkBan()
				if not ret:
					self.__checkUnBan()
					time.sleep(self.getSleepTime())
			else:
				time.sleep(self.getSleepTime())
		self.__flushBan()
		for name, action in self._actions.iteritems():
			try:
				action.stop()
			except Exception as e:
				logSys.error("Failed to stop jail '%s' action '%s': %s",
					self._jail.getName(), name, e)
		logSys.debug(self._jail.getName() + ": action terminated")
		return True

	def __checkBan(self):
		"""Check for IP address to ban.

		Look in the jail queue for FailTicket. If a ticket is available,
		it executes the "ban" command and adds a ticket to the BanManager.

		Returns
		-------
		bool
			True if an IP address get banned.
		"""
		ticket = self._jail.getFailTicket()
		if ticket != False:
			aInfo = CallingMap()
			bTicket = BanManager.createBanTicket(ticket)
			aInfo["ip"] = bTicket.getIP()
			aInfo["failures"] = bTicket.getAttempt()
			aInfo["time"] = bTicket.getTime()
			aInfo["matches"] = "\n".join(bTicket.getMatches())
			if self._jail.getDatabase() is not None:
				aInfo["ipmatches"] = lambda: "\n".join(
					self._jail.getDatabase().getBansMerged(
						ip=bTicket.getIP()).getMatches())
				aInfo["ipjailmatches"] = lambda: "\n".join(
					self._jail.getDatabase().getBansMerged(
						ip=bTicket.getIP(), jail=self._jail).getMatches())
				aInfo["ipfailures"] = lambda: "\n".join(
					self._jail.getDatabase().getBansMerged(
						ip=bTicket.getIP()).getAttempt())
				aInfo["ipjailfailures"] = lambda: "\n".join(
					self._jail.getDatabase().getBansMerged(
						ip=bTicket.getIP(), jail=self._jail).getAttempt())
			if self.__banManager.addBanTicket(bTicket):
				logSys.warning("[%s] Ban %s" % (self._jail.getName(), aInfo["ip"]))
				for name, action in self._actions.iteritems():
					try:
						action.ban(aInfo)
					except Exception as e:
						logSys.error(
							"Failed to execute ban jail '%s' action '%s': %s",
							self._jail.getName(), name, e)
				return True
			else:
				logSys.info("[%s] %s already banned" % (self._jail.getName(),
														aInfo["ip"]))
		return False

	def __checkUnBan(self):
		"""Check for IP address to unban.

		Unban IP addresses which are outdated.
		"""
		for ticket in self.__banManager.unBanList(MyTime.time()):
			self.__unBan(ticket)

	def __flushBan(self):
		"""Flush the ban list.

		Unban all IP address which are still in the banning list.
		"""
		logSys.debug("Flush ban list")
		for ticket in self.__banManager.flushBanList():
			self.__unBan(ticket)

	def __unBan(self, ticket):
		"""Unbans host corresponding to the ticket.

		Executes the actions in order to unban the host given in the
		ticket.

		Parameters
		----------
		ticket : FailTicket
			Ticket of failures of which to unban
		"""
		aInfo = dict()
		aInfo["ip"] = ticket.getIP()
		aInfo["failures"] = ticket.getAttempt()
		aInfo["time"] = ticket.getTime()
		aInfo["matches"] = "".join(ticket.getMatches())
		logSys.warning("[%s] Unban %s" % (self._jail.getName(), aInfo["ip"]))
		for name, action in self._actions.iteritems():
			try:
				action.unban(aInfo)
			except Exception as e:
				logSys.error(
					"Failed to execute unban jail '%s' action '%s': %s",
					self._jail.getName(), name, e)

	def status(self):
		"""Get the status of the filter.

		Get some informations about the filter state such as the total
		number of failures.

		Returns
		-------
		list
			List of tuple pairs, each containing a description and value
			for general status information.
		"""
		ret = [("Currently banned", self.__banManager.size()), 
			   ("Total banned", self.__banManager.getBanTotal()),
			   ("IP list", self.__banManager.getBanList())]
		return ret